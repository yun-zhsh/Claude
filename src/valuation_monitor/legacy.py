"""Import from and export to the file layout used before the SQLite database (data/prices, data/fx, data/eps as CSV and JSON files).

The export is also what the "All raw data (zip)" download contains, so people can still open the data in Excel. The layout:

  prices/<symbol>/tencent.csv, tencent.json, yahoo.csv, yahoo.json, final.csv
  fx/<pair>.csv, <pair>.json
  eps/<symbol>_announcements.csv, <symbol>_periods.csv, <symbol>_ttm.csv

Command line (valuation-monitor-db):
  import-csv DIR   read that layout into the database (the one-time migration; also useful to load a backup)
  export-csv DIR   write the database in that layout
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from valuation_monitor.db import FINAL_COLUMNS, PRICE_COLUMNS, Database

EPS_KINDS = ("announcements", "periods", "ttm")
CSV_KW = {"index_label": "date", "date_format": "%Y-%m-%d"}


def _bars_csv(df: pd.DataFrame) -> bytes:
    if "volume" in df.columns:
        df = df.assign(volume=df["volume"].astype("Int64"))
    return df.to_csv(**CSV_KW).encode("utf-8")


def export_files(db: Database, symbol: str | None = None) -> dict[str, bytes]:
    """The data in the legacy layout as {relative path: content}. All FX pairs are included; prices and earnings only for `symbol` (all symbols if None)."""
    out: dict[str, bytes] = {}
    symbols = [symbol] if symbol else sorted(set(db.symbols("prices_final")) | set(db.symbols("periods")) | set(db.symbols("ttm")))
    for sym in symbols:
        for source in ("tencent", "yahoo"):
            df = db.read_series("price_bars", {"symbol": sym, "source": source}, PRICE_COLUMNS[source])
            if not df.empty:
                out[f"prices/{sym}/{source}.csv"] = _bars_csv(df)
            scope = f"prices/{sym}/{source}"
            if db.meta_text(scope):
                out[f"{scope}.json"] = db.meta_text(scope).encode("utf-8")
        final = db.read_series("prices_final", {"symbol": sym}, FINAL_COLUMNS)
        if not final.empty:
            out[f"prices/{sym}/final.csv"] = final.to_csv(**CSV_KW).encode("utf-8")
        for kind in EPS_KINDS:
            df = db.read_frame(kind, sym, text=(kind == "announcements"))
            if df is not None:
                out[f"eps/{sym}_{kind}.csv"] = df.to_csv(index=False, date_format="%Y-%m-%d").encode("utf-8")
    for scope in db.meta_scopes("fx/"):
        pair = scope.split("/", 1)[1]
        rates = db.read_series("fx_rates", {"pair": pair}, ["rate"])
        if not rates.empty:
            out[f"fx/{pair}.csv"] = rates.to_csv(**CSV_KW).encode("utf-8")
        out[f"fx/{pair}.json"] = db.meta_text(scope).encode("utf-8")
    return dict(sorted(out.items()))


def export_to(db: Database, folder: str | Path) -> int:
    folder = Path(folder)
    files = export_files(db)
    for rel, content in files.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return len(files)


def import_from(db: Database, folder: str | Path) -> int:
    """Read the legacy layout into the database; returns the number of files read. Rows that differ are updated, nothing is deleted, and a second import of the same files changes nothing."""
    folder = Path(folder)
    n = 0
    for sym_dir in sorted((folder / "prices").glob("*")):
        if not sym_dir.is_dir():
            continue
        sym = sym_dir.name
        for source in ("tencent", "yahoo"):
            csv_path = sym_dir / f"{source}.csv"
            if csv_path.exists():
                df = pd.read_csv(csv_path, index_col="date", parse_dates=True)
                db.write_series("price_bars", {"symbol": sym, "source": source}, df[PRICE_COLUMNS[source]])
                n += 1
            meta = sym_dir / f"{source}.json"
            if meta.exists():
                db.write_meta(f"prices/{sym}/{source}", json.loads(meta.read_text(encoding="utf-8")))
                n += 1
        final = sym_dir / "final.csv"
        if final.exists():
            df = pd.read_csv(final, index_col="date", parse_dates=True)
            db.write_series("prices_final", {"symbol": sym}, df[FINAL_COLUMNS])
            n += 1
    for csv_path in sorted((folder / "fx").glob("*.csv")):
        pair = csv_path.stem
        db.write_series("fx_rates", {"pair": pair}, pd.read_csv(csv_path, index_col="date", parse_dates=True)[["rate"]])
        meta = csv_path.with_suffix(".json")
        if meta.exists():
            db.write_meta(f"fx/{pair}", json.loads(meta.read_text(encoding="utf-8")))
        n += 1
    for kind in EPS_KINDS:
        for csv_path in sorted((folder / "eps").glob(f"*_{kind}.csv")):
            sym = csv_path.name[: -len(f"_{kind}.csv")]
            if kind == "announcements":
                db.write_frame(kind, sym, pd.read_csv(csv_path, dtype=str), text=True)
            else:
                db.write_frame(kind, sym, pd.read_csv(csv_path), date_format="%Y-%m-%d")
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="valuation-monitor-db", description="Import or export the saved data in the CSV/JSON layout used before the SQLite database")
    parser.add_argument("command", choices=["import-csv", "export-csv"])
    parser.add_argument("folder", help="Folder holding (import) or receiving (export) the prices/, fx/ and eps/ folders")
    parser.add_argument("--data-dir", default="data", help="Directory with valuation.db (default: data)")
    args = parser.parse_args(argv)
    db = Database(Path(args.data_dir) / "valuation.db")
    if args.command == "import-csv":
        print(f"Imported {import_from(db, args.folder)} files into {db.path}")
    else:
        print(f"Exported {export_to(db, args.folder)} files to {args.folder}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
