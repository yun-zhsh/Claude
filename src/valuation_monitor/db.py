"""The SQLite database that holds everything the program saves (data/valuation.db).

Tables:
  price_bars       raw daily bars of each price source (symbol, source = tencent | yahoo)
  fx_rates         daily FX rates (pair, e.g. CNYHKD)
  prices_final     the closing price finally used on each day, with source and cross-check status
  meta             one JSON document per series (source note, where the download starts, currency, splits)
  announcements    parse result of every earnings filing        \\
  periods          earnings per period                           > derived tables: they mirror the columns of the data
  ttm              per-share data used for valuation             /  frames built in filings.py, one set of rows per symbol

The original filings (PDF/HTML) stay as files in data/filings/; they are large and are never queried.

Design rules that keep the file small in git (it is committed after every scheduled update):
  - only what changed is written: new bars are inserted, existing rows are touched only when their values differ,
    and the derived tables are rewritten only when their content changed;
  - the file is never rebuilt or vacuumed by the program, and the journal mode is DELETE (no -wal file).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pandas as pd

SCHEMA_VERSION = 1

TABLES = {
    "price_bars": (
        "CREATE TABLE price_bars (symbol TEXT NOT NULL, source TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, "
        "close REAL, adjclose REAL, volume INTEGER, UNIQUE (symbol, source, date))"
    ),
    "fx_rates": "CREATE TABLE fx_rates (pair TEXT NOT NULL, date TEXT NOT NULL, rate REAL, UNIQUE (pair, date))",
    "prices_final": (
        "CREATE TABLE prices_final (symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL, source TEXT, status TEXT, tencent_close REAL, "
        "yahoo_close_unadjusted REAL, yahoo_factor REAL, UNIQUE (symbol, date))"
    ),
    "meta": "CREATE TABLE meta (scope TEXT PRIMARY KEY, value TEXT NOT NULL)",
}
FRAME_TABLES = ("announcements", "periods", "ttm")  # derived tables, created from the columns of the frame written to them

# column order of the legacy CSV files (and of the series a caller asks for)
PRICE_COLUMNS = {
    "tencent": ["open", "high", "low", "close", "volume"],
    "yahoo": ["open", "high", "low", "close", "adjclose", "volume"],
}
FINAL_COLUMNS = ["close", "source", "status", "tencent_close", "yahoo_close_unadjusted", "yahoo_factor"]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _python(value):
    """A pandas/numpy value as something sqlite3 accepts (NaN, NaT and NA become NULL)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return value.item() if hasattr(value, "item") else value


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} was written by a newer version of the program (schema {version}, this one knows {SCHEMA_VERSION})")
            if version < SCHEMA_VERSION:
                for sql in TABLES.values():
                    conn.execute(sql.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def connect(self) -> sqlite3.Connection:
        """A new connection (cheap); `with db.connect() as conn` commits or rolls back the transaction."""
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode = DELETE")
        return conn

    # ---- meta documents -------------------------------------------------------------------------------------------------

    def read_meta(self, scope: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE scope = ?", (scope,)).fetchone()
        return json.loads(row[0]) if row else {}

    def meta_text(self, scope: str) -> str:
        """The saved JSON text of a meta document ("" if there is none)."""
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE scope = ?", (scope,)).fetchone()
        return row[0] if row else ""

    def write_meta(self, scope: str, meta: dict) -> None:
        text = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE scope = ?", (scope,)).fetchone()
            if row is None or row[0] != text:
                conn.execute("INSERT OR REPLACE INTO meta (scope, value) VALUES (?, ?)", (scope, text))

    def meta_scopes(self, prefix: str = "") -> list[str]:
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT scope FROM meta WHERE scope LIKE ? ORDER BY scope", (prefix + "%",))]

    # ---- daily series (bars, FX rates, final prices) ---------------------------------------------------------------------

    def read_series(self, table: str, keys: dict[str, str], columns: list[str]) -> pd.DataFrame:
        """The rows of one series (e.g. price_bars with symbol and source) as a frame indexed by date (a DatetimeIndex named "date")."""
        where = " AND ".join(f"{_quote(k)} = ?" for k in keys)
        sql = f"SELECT date, {', '.join(_quote(c) for c in columns)} FROM {table} WHERE {where} ORDER BY date"
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(keys.values())).fetchall()
        if not rows:
            return pd.DataFrame(columns=columns, dtype=float, index=pd.DatetimeIndex([], name="date"))
        df = pd.DataFrame(rows, columns=["date", *columns])
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")

    def write_series(self, table: str, keys: dict[str, str], df: pd.DataFrame, replace: bool = False, prune: bool = False) -> int:
        """Save the rows of a series. Only rows that are new, or whose values differ, are written (all rows if `replace`: the series is deleted first).

        prune=True also deletes saved rows whose date is not in `df` (for results that are recomputed on every run).
        Returns the number of rows written or deleted.
        """
        columns = list(df.columns)
        key_sql = " AND ".join(f"{_quote(k)} = ?" for k in keys)
        names = [*keys, "date", *columns]
        insert = f"INSERT OR REPLACE INTO {table} ({', '.join(_quote(n) for n in names)}) VALUES ({', '.join('?' * len(names))})"
        written = 0
        with self._lock, self.connect() as conn:
            if replace:
                conn.execute(f"DELETE FROM {table} WHERE {key_sql}", tuple(keys.values()))
                stored = {}
            else:
                select = f"SELECT date, {', '.join(_quote(c) for c in columns)} FROM {table} WHERE {key_sql}"
                stored = {r[0]: tuple(r[1:]) for r in conn.execute(select, tuple(keys.values()))}
            for day, row in zip(df.index, df.itertuples(index=False)):
                date = f"{pd.Timestamp(day):%Y-%m-%d}"
                values = tuple(_python(v) for v in row)
                if stored.get(date) == values:
                    continue
                conn.execute(insert, (*keys.values(), date, *values))
                written += 1
            if prune and not replace:
                gone = set(stored) - {f"{pd.Timestamp(d):%Y-%m-%d}" for d in df.index}
                for date in gone:
                    conn.execute(f"DELETE FROM {table} WHERE {key_sql} AND date = ?", (*keys.values(), date))
                    written += 1
        return written

    # ---- derived tables (frames) ----------------------------------------------------------------------------------------

    def read_frame(self, table: str, symbol: str, text: bool = False) -> pd.DataFrame | None:
        """The rows of a derived table for one symbol in the order they were written; None if there are none.

        text=True returns every value as a string (or None), for tables the program itself reads back as text.
        """
        with self.connect() as conn:
            if not self._exists(conn, table):
                return None
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "symbol"]
            rows = conn.execute(f"SELECT {', '.join(_quote(c) for c in cols)} FROM {table} WHERE symbol = ? ORDER BY rowid", (symbol,)).fetchall()
        if not rows:
            return None
        if text:
            return pd.DataFrame([[None if v is None else str(v) for v in r] for r in rows], columns=cols, dtype=object)
        return pd.DataFrame(rows, columns=cols)

    def write_frame(self, table: str, symbol: str, df: pd.DataFrame, text: bool = False, date_format: str | None = None) -> bool:
        """Save the rows of a derived table for one symbol, only if they differ from what is saved. Returns whether anything was written.

        Datetime columns are stored as text ("%Y-%m-%d" unless date_format is given), like the CSV files did.
        """
        df = df.copy()
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].dt.strftime(date_format or "%Y-%m-%d")
        saved = self.read_frame(table, symbol, text=text)
        new_text = df.to_csv(index=False, date_format=date_format)
        if saved is not None and saved.to_csv(index=False, date_format=date_format) == new_text:
            return False
        rows = [[(None if _python(v) is None else (str(v) if text else _python(v))) for v in r] for r in df.itertuples(index=False)]
        with self._lock, self.connect() as conn:
            cols = list(df.columns)
            if self._exists(conn, table):
                have = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "symbol"]
                if have != cols:  # the code changed the columns: these tables are derived, so rebuild them
                    conn.execute(f"DROP TABLE {table}")
            if not self._exists(conn, table):
                conn.execute(f"CREATE TABLE {table} (symbol TEXT NOT NULL, {', '.join(_quote(c) for c in cols)})")  # no column types: values keep their type
                conn.execute(f"CREATE INDEX {table}_symbol ON {table} (symbol)")
            conn.execute(f"DELETE FROM {table} WHERE symbol = ?", (symbol,))
            conn.executemany(
                f"INSERT INTO {table} (symbol, {', '.join(_quote(c) for c in cols)}) VALUES ({', '.join('?' * (len(cols) + 1))})",
                [(symbol, *r) for r in rows],
            )
        return True

    def symbols(self, table: str = "prices_final") -> list[str]:
        with self.connect() as conn:
            if not self._exists(conn, table):
                return []
            return [r[0] for r in conn.execute(f"SELECT DISTINCT symbol FROM {table} ORDER BY symbol")]

    @staticmethod
    def _exists(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None

    def has_data(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM prices_final LIMIT 1").fetchone() is not None
