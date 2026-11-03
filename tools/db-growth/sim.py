"""Experiment: how much does a git repository grow when the data lives in different forms?

Usage: python sim.py VARIANT REAL_DATA_DIR WORK_DIR [COPIES] [DAYS]
  REAL_DATA_DIR  the real data in the CSV/JSON layout (prices/, fx/, eps/): make it with
                 `python -m valuation_monitor.legacy export-csv DIR`
  Run the variants in parallel, then measure with measure.sh. See docs/database-growth.md.
  VARIANT   db      the SQLite file itself is committed (changed in place)
            sql     a text dump of the database (one file, regenerated in table order) is committed
            dbrw    the SQLite file, and derived tables (periods, ttm, prices_final) are deleted and rewritten on every run
            dbfresh the SQLite file is rebuilt from scratch on every run (VACUUM INTO a new file)
            csvtab  one CSV per table (all symbols in the file)
            csvsym  today's layout: one CSV per symbol and source
  COPIES    how many perturbed copies of the 5 real companies (10 -> 50 companies)
  DAYS      business days to simulate (2 commits per day: Asia stocks, then US stock)

Real data is the 5 tracked companies; copies get random per-row noise so they do not compress
unrealistically well. Each simulated day appends one price row per symbol and source; every
symbol also files 4 earnings filings a year (a row each in announcements, periods and ttm).
"""

import csv
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

variant, real_dir, work_dir = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
COPIES = int(sys.argv[4]) if len(sys.argv) > 4 else 10
DAYS = int(sys.argv[5]) if len(sys.argv) > 5 else 520
rng = random.Random(12345)
NUM_RE = re.compile(r"\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+")

BASE = ["0700.HK", "2382.HK", "JD", "005930.KS", "002415.SZ"]
US = {"JD"}
HK = {"0700.HK", "2382.HK"}

TABLES = {  # name -> columns (symbol first)
    "price_bars": ["symbol", "source", "date", "open", "high", "low", "close", "adjclose", "volume"],
    "prices_final": ["symbol", "date", "close", "source", "status", "tencent_close", "yahoo_close_unadjusted", "yahoo_factor"],
    "fx_rates": ["pair", "date", "rate"],
}


def typed(df: pd.DataFrame) -> list[list]:
    """Rows as python values: numbers as int/float, empty as None, everything else str."""
    out = []
    df = df.convert_dtypes()
    for row in df.itertuples(index=False):
        out.append([None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in row])
    return out


def jitter_cell(v, f):
    if isinstance(v, float):
        return round(v * f, 4)
    if isinstance(v, int) and not isinstance(v, bool):
        return int(v * f)
    if isinstance(v, str):
        return NUM_RE.sub(lambda m: f"{float(m.group().replace(',', '')) * f:,.4f}".rstrip("0").rstrip("."), v)
    return v


def load_base():
    """tables[name] = list of rows (python values); columns[name] = column names."""
    tables = {k: [] for k in TABLES}
    columns = dict(TABLES)
    eps_cols = {}
    for kind in ("announcements", "periods", "ttm"):
        tables[kind] = []
    for copy in range(COPIES):
        for s in BASE:
            sym = s if copy == 0 else f"{s}~{copy}"
            f_sym = 1.0 if copy == 0 else math.exp(rng.gauss(0, 0.4))
            for source in ("tencent", "yahoo"):
                p = real_dir / "prices" / s / f"{source}.csv"
                if not p.exists():
                    continue
                df = pd.read_csv(p)
                for r in typed(df):
                    d = dict(zip(df.columns, r))
                    f = 1.0 if copy == 0 else f_sym * math.exp(rng.gauss(0, 0.015))
                    price = lambda c: None if d.get(c) is None else round(d[c] * f, 4)
                    tables["price_bars"].append([sym, source, d["date"], price("open"), price("high"), price("low"), price("close"),
                                                 price("adjclose") if "adjclose" in d else None,
                                                 int(d["volume"] * rng.uniform(0.5, 1.5)) if copy else d["volume"]])
            df = pd.read_csv(real_dir / "prices" / s / "final.csv")
            for r in typed(df):
                f = 1.0 if copy == 0 else f_sym * math.exp(rng.gauss(0, 0.015))
                row = [sym] + [jitter_cell(v, f) if isinstance(v, float) and i != 6 else v for i, v in enumerate(r)]
                tables["prices_final"].append(row)
            for kind in ("announcements", "periods", "ttm"):
                df = pd.read_csv(real_dir / "eps" / f"{s}_{kind}.csv")
                eps_cols[kind] = ["symbol"] + list(df.columns)
                for r in typed(df):
                    f = 1.0 if copy == 0 else f_sym * math.exp(rng.gauss(0, 0.05))
                    tables[kind].append([sym] + [v if i < 3 and kind != "announcements" else jitter_cell(v, f) for i, v in enumerate(r)])
    for pair in ("CNYHKD", "CNYUSD"):
        df = pd.read_csv(real_dir / "fx" / f"{pair}.csv")
        for r in typed(df):
            tables["fx_rates"].append([pair] + r)
    columns.update(eps_cols)
    return tables, columns


def sql_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def sql_line(table, row):
    return f"INSERT INTO \"{table}\" VALUES({','.join(sql_value(v) for v in row)});\n"


def csv_line(row):
    buf = []
    for v in row:
        if v is None:
            buf.append("")
        elif isinstance(v, str) and any(c in v for c in ',"\n'):
            buf.append('"' + v.replace('"', '""') + '"')
        else:
            buf.append(str(v))
    return ",".join(buf) + "\n"


def create_sql(table, cols, rows):
    def kind(i):
        vals = [r[i] for r in rows[:200] if r[i] is not None]
        if vals and all(isinstance(v, int) for v in vals):
            return "INTEGER"
        if vals and all(isinstance(v, (int, float)) for v in vals):
            return "REAL"
        return "TEXT"
    return f'CREATE TABLE "{table}" (' + ", ".join(f'"{c}" {kind(i)}' for i, c in enumerate(cols)) + ");\n"


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pack_mb(cwd):
    out = subprocess.run(["git", "count-objects", "-v"], cwd=cwd, capture_output=True, text=True).stdout
    kv = dict(line.split(": ") for line in out.strip().splitlines())
    return (int(kv["size-pack"]) + int(kv["size"])) / 1024  # KiB -> MiB


def main():
    repo = work_dir / variant
    if repo.exists():
        subprocess.run(["rm", "-rf", str(repo)])
    repo.mkdir(parents=True)
    git("init", "-q", cwd=repo)
    git("config", "user.email", "x@x", cwd=repo)
    git("config", "user.name", "x", cwd=repo)
    git("config", "gc.auto", "0", cwd=repo)

    tables, columns = load_base()
    ddl = {t: create_sql(t, columns[t], tables[t]) for t in tables}
    n_rows0 = {t: len(r) for t, r in tables.items()}

    # ---- initial files
    DBV = variant in ("db", "dbrw", "dbfresh")
    dbpath = (work_dir / f"{variant}_scratch.db") if variant == "dbfresh" else (repo / "valuation.db")
    if dbpath.exists():
        dbpath.unlink()
    db = sqlite3.connect(dbpath) if DBV else None
    sql_lines = {t: [sql_line(t, r) for r in rows] for t, rows in tables.items()} if variant == "sql" else None

    def write_all():
        if DBV:
            for t, rows in tables.items():
                db.execute(ddl[t].replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
                db.executemany(f"INSERT INTO \"{t}\" VALUES({','.join('?' * len(columns[t]))})", rows)
            db.execute("CREATE UNIQUE INDEX ix_bars ON price_bars(symbol, source, date)")
            db.execute("CREATE UNIQUE INDEX ix_final ON prices_final(symbol, date)")
            db.execute("CREATE INDEX ix_ann ON announcements(symbol)")
            db.commit()
        elif variant == "sql":
            write_sql()
        elif variant == "csvtab":
            (repo / "tables").mkdir()
            for t, rows in tables.items():
                with open(repo / "tables" / f"{t}.csv", "w", newline="") as fh:
                    fh.write(",".join(columns[t]) + "\n")
                    fh.writelines(csv_line(r) for r in rows)
        elif variant == "csvsym":
            groups = {}
            for t, rows in tables.items():
                for r in rows:
                    key = csv_path(t, r)
                    groups.setdefault(key, (t, [])) [1].append(r)
            for key, (t, rows) in groups.items():
                p = repo / key
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "w", newline="") as fh:
                    fh.write(",".join(columns[t][1:]) + "\n")
                    fh.writelines(csv_line(r[1:]) for r in rows)

    def csv_path(t, r):
        sym = r[0]
        base = sym.split("~")[0]
        folder = sym.replace("~", "_")
        if t == "price_bars":
            return f"prices/{folder}/{r[1]}.csv"
        if t == "prices_final":
            return f"prices/{folder}/final.csv"
        if t == "fx_rates":
            return f"fx/{sym}.csv"
        return f"eps/{folder}_{t}.csv"

    def write_sql():
        with open(repo / "valuation.sql", "w") as fh:
            for t in sorted(tables):
                fh.write(ddl[t])
            for t in sorted(tables):
                fh.writelines(sql_lines[t])

    write_all()
    if variant == "dbfresh":
        db.execute("VACUUM INTO ?", (str(repo / "valuation.db"),))
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "initial", cwd=repo)

    # ---- simulation state
    last = {}
    for r in tables["price_bars"]:
        last[(r[0], r[1])] = r
    last_final = {r[0]: r for r in tables["prices_final"]}
    symbols = sorted({r[0] for r in tables["price_bars"]})
    by_src = {s: sorted({k[1] for k in last if k[0] == s}) for s in symbols}
    base_of = lambda s: s.split("~")[0]
    day = date(2026, 11, 3)
    filing_days = {s: {rng.randrange(DAYS) for _ in range(4 * DAYS // 250 + 1)} for s in symbols}
    results = []
    commit_no = 0
    t0 = time.time()

    def add_row(t, row):
        tables[t].append(row)
        if DBV:
            db.execute(f"INSERT INTO \"{t}\" VALUES({','.join('?' * len(row))})", row)
        elif variant == "sql":
            sql_lines[t].append(sql_line(t, row))
        elif variant == "csvtab":
            with open(repo / "tables" / f"{t}.csv", "a", newline="") as fh:
                fh.write(csv_line(row))
        elif variant == "csvsym":
            with open(repo / csv_path(t, row), "a", newline="") as fh:
                fh.write(csv_line(row[1:]))

    def bar_row(sym, source):
        p = last[(sym, source)]
        f = math.exp(rng.gauss(0, 0.015))
        px = lambda v: None if v is None else round(v * f, 4)
        row = [sym, source, d_str, px(p[3]), px(p[4]), px(p[5]), px(p[6]), px(p[7]), max(1, int(p[8] * rng.uniform(0.6, 1.4)))]
        last[(sym, source)] = row
        return row

    def new_filing(sym, idx):
        for kind in ("announcements", "periods", "ttm"):
            mine = [r for r in tables[kind] if r[0] == sym]
            row = list(rng.choice(mine))
            f = math.exp(rng.gauss(0, 0.03))
            row = [row[0]] + [jitter_cell(v, f) for v in row[1:]]
            if kind == "announcements":
                row[1] = f"{d_str} 17:00"
            add_row(kind, row)

    checkpoints = {100, 300, 520, 780, 1040}
    sim_day = 0
    while sim_day < DAYS:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        d_str = day.isoformat()
        for group in (lambda s: base_of(s) not in US, lambda s: base_of(s) in US):
            for sym in symbols:
                if not group(sym):
                    continue
                for source in by_src[sym]:
                    add_row("price_bars", bar_row(sym, source))
                lf = last_final[sym]
                src = "tencent" if base_of(sym) in HK else "yahoo"
                close = last[(sym, src)][6]
                row = [sym, d_str, close, lf[3], lf[4], close if src == "tencent" else None, last[(sym, "yahoo")][6], lf[7]]
                last_final[sym] = row
                add_row("prices_final", row)
                if sim_day in filing_days[sym]:
                    new_filing(sym, sim_day)
            for pair in ("CNYHKD", "CNYUSD"):
                pass
            if DBV:
                if variant == "dbrw":
                    for tname in ("periods", "ttm", "prices_final"):
                        db.execute(f'DELETE FROM "{tname}"')
                        db.executemany(f'INSERT INTO "{tname}" VALUES({",".join("?" * len(columns[tname]))})', tables[tname])
                db.commit()
                if variant == "dbfresh":
                    out = repo / "valuation.db"
                    if out.exists():
                        out.unlink()
                    db.execute("VACUUM INTO ?", (str(out),))
            elif variant == "sql":
                write_sql()
            git("add", "-A", cwd=repo)
            git("commit", "-q", "-m", f"update {d_str}", cwd=repo)
            commit_no += 1
            if commit_no in checkpoints or commit_no % 100 == 0:
                if DBV:
                    db.commit()
                git("repack", "-a", "-d", "-q", cwd=repo)
                git("prune-packed", cwd=repo)
                tree_mb = sum(p.stat().st_size for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts) / 1e6
                rec = {"variant": variant, "commits": commit_no, "git_MB": round(pack_mb(repo) * 1.048576, 1), "tree_MB": round(tree_mb, 1),
                       "rows": sum(len(r) for r in tables.values()), "seconds": round(time.time() - t0)}
                results.append(rec)
                print(json.dumps(rec), flush=True)
        sim_day += 1
    if DBV:
        db.close()
    (work_dir / f"{variant}.json").write_text(json.dumps(results))


main()
