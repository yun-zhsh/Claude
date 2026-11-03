"""The SQLite database: only what changed is written, nothing is lost on a round trip, and the file stays stable."""

import hashlib
import sqlite3

import pandas as pd
import pytest

from valuation_monitor.db import FINAL_COLUMNS, PRICE_COLUMNS, SCHEMA_VERSION, Database
from valuation_monitor.legacy import export_files, export_to, import_from


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bars(closes, start="2024-01-02", adjclose=False):
    idx = pd.date_range(start, periods=len(closes), freq="D", name="date")
    df = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes}, index=idx, dtype=float)
    if adjclose:
        df["adjclose"] = df["close"] * 0.5
    df["volume"] = pd.array([100 + i for i in range(len(closes))], dtype="Int64")
    return df


def test_series_only_new_or_changed_rows_are_written(tmp_path):
    db = Database(tmp_path / "v.db")
    key = {"symbol": "0700.HK", "source": "tencent"}
    cols = PRICE_COLUMNS["tencent"]
    assert db.write_series("price_bars", key, bars([1.0, 2.0, 3.0])[cols]) == 3
    assert db.write_series("price_bars", key, bars([1.0, 2.0, 3.0])[cols]) == 0  # same data: nothing is written
    assert db.write_series("price_bars", key, bars([1.0, 2.0, 3.0, 4.0])[cols]) == 1  # one new day
    changed = bars([1.0, 2.5, 3.0, 4.0])[cols]
    assert db.write_series("price_bars", key, changed) == 1  # one value differs
    got = db.read_series("price_bars", key, cols)
    assert got["close"].tolist() == [1.0, 2.5, 3.0, 4.0] and got["volume"].tolist() == [100, 101, 102, 103]
    assert got.index.name == "date" and isinstance(got.index, pd.DatetimeIndex)

    other = {"symbol": "JD", "source": "yahoo"}
    db.write_series("price_bars", other, bars([9.0], adjclose=True)[PRICE_COLUMNS["yahoo"]])
    assert len(db.read_series("price_bars", key, cols)) == 4  # series do not mix
    db.write_series("price_bars", key, bars([7.0])[cols], replace=True)
    assert db.read_series("price_bars", key, cols)["close"].tolist() == [7.0]  # replace drops the old rows
    assert len(db.read_series("price_bars", other, PRICE_COLUMNS["yahoo"])) == 1


def test_prune_removes_days_that_are_no_longer_in_the_result(tmp_path):
    db = Database(tmp_path / "v.db")
    final = pd.DataFrame(
        {"close": [1.0, 2.0], "source": ["tencent"] * 2, "status": ["agree"] * 2, "tencent_close": [1.0, 2.0], "yahoo_close_unadjusted": [1.0, 2.0], "yahoo_factor": [1.0, 1.0]},
        index=pd.date_range("2024-01-02", periods=2, name="date"),
    )
    db.write_series("prices_final", {"symbol": "X"}, final, prune=True)
    assert db.write_series("prices_final", {"symbol": "X"}, final.iloc[:1], prune=True) == 1
    assert len(db.read_series("prices_final", {"symbol": "X"}, FINAL_COLUMNS)) == 1


def test_missing_values_are_stored_as_null_and_come_back_as_nan(tmp_path):
    db = Database(tmp_path / "v.db")
    df = bars([1.0, 2.0], adjclose=True)[PRICE_COLUMNS["yahoo"]]
    df.loc[df.index[1], "adjclose"] = float("nan")
    db.write_series("price_bars", {"symbol": "A", "source": "yahoo"}, df)
    got = db.read_series("price_bars", {"symbol": "A", "source": "yahoo"}, PRICE_COLUMNS["yahoo"])
    assert got["adjclose"].isna().tolist() == [False, True]
    with sqlite3.connect(tmp_path / "v.db") as conn:
        assert conn.execute("SELECT adjclose FROM price_bars WHERE date = '2024-01-03'").fetchone() == (None,)


def test_frames_are_rewritten_only_when_the_content_changed(tmp_path):
    db = Database(tmp_path / "v.db")
    df = pd.DataFrame({"period": ["2024Q1", "2024Q2"], "eps": [1.5, 2.0], "count": [0, 1], "note": [None, "x"]})
    assert db.write_frame("periods", "A", df) is True
    assert db.write_frame("periods", "A", df) is False
    got = db.read_frame("periods", "A")
    assert got["period"].tolist() == ["2024Q1", "2024Q2"] and got["eps"].tolist() == [1.5, 2.0] and got["count"].tolist() == [0, 1]
    db.write_frame("periods", "B", df.assign(eps=[9.0, 9.5]))
    assert db.read_frame("periods", "A")["eps"].tolist() == [1.5, 2.0]  # symbols do not mix
    assert db.write_frame("periods", "A", df.assign(eps=[1.5, 2.5])) is True
    assert db.read_frame("periods", "A")["eps"].tolist() == [1.5, 2.5]
    assert db.read_frame("periods", "nobody") is None and db.read_frame("ttm", "A") is None

    # a changed set of columns (the code changed): the derived table is rebuilt
    db.write_frame("periods", "A", df.assign(extra=[1, 2]))
    assert "extra" in db.read_frame("periods", "A").columns


def test_text_frames_keep_every_value_as_a_string(tmp_path):
    db = Database(tmp_path / "v.db")
    df = pd.DataFrame({"eps": ["1.948;1.264", "0.5"], "provisional": ["0", "1"], "parser_version": ["5", "5"]})
    db.write_frame("announcements", "A", df, text=True)
    got = db.read_frame("announcements", "A", text=True)
    assert got["parser_version"].tolist() == ["5", "5"] and got["eps"].tolist() == ["1.948;1.264", "0.5"]
    assert db.write_frame("announcements", "A", df, text=True) is False


def test_datetime_columns_are_saved_as_date_text(tmp_path):
    db = Database(tmp_path / "v.db")
    df = pd.DataFrame({"report_date": pd.to_datetime(["2024-03-31", None]), "v": [1.0, 2.0]})
    db.write_frame("ttm", "A", df, date_format="%Y-%m-%d")
    assert db.read_frame("ttm", "A")["report_date"].tolist()[0] == "2024-03-31"
    assert db.write_frame("ttm", "A", df, date_format="%Y-%m-%d") is False


def test_meta_documents_are_saved_exactly_and_written_only_on_change(tmp_path):
    db = Database(tmp_path / "v.db")
    db.write_meta("prices/A/yahoo", {"source": "Yahoo", "splits": {"2018-05-04": 50.0}})
    assert db.read_meta("prices/A/yahoo") == {"source": "Yahoo", "splits": {"2018-05-04": 50.0}}
    before = db.meta_text("prices/A/yahoo")
    db.write_meta("prices/A/yahoo", {"source": "Yahoo", "splits": {"2018-05-04": 50.0}})
    assert db.meta_text("prices/A/yahoo") == before and before.endswith("}\n")
    assert db.read_meta("nothing") == {} and db.meta_text("nothing") == ""
    assert db.meta_scopes("prices/") == ["prices/A/yahoo"]


def test_a_database_from_a_newer_program_is_refused(tmp_path):
    path = tmp_path / "v.db"
    Database(path)
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer version"):
        Database(path)


def test_the_file_is_a_single_file_and_unchanged_by_repeated_identical_writes(tmp_path):
    db = Database(tmp_path / "v.db")
    key = {"symbol": "A", "source": "tencent"}
    df = bars([1.0, 2.0, 3.0])[PRICE_COLUMNS["tencent"]]
    db.write_series("price_bars", key, df)
    db.write_frame("periods", "A", pd.DataFrame({"p": ["1"], "v": [1.0]}))
    db.write_meta("m", {"a": 1})
    before = sha(tmp_path / "v.db")
    for _ in range(3):
        db.write_series("price_bars", key, df)
        db.write_frame("periods", "A", pd.DataFrame({"p": ["1"], "v": [1.0]}))
        db.write_meta("m", {"a": 1})
    assert sha(tmp_path / "v.db") == before  # nothing was written, so git sees no change
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v.db"]  # no -wal / -journal files left behind


def legacy_folder(root):
    """A small data folder in the layout used before the database."""
    (root / "prices" / "T.HK").mkdir(parents=True)
    (root / "fx").mkdir()
    (root / "eps").mkdir()
    (root / "prices" / "T.HK" / "tencent.csv").write_text("date,open,high,low,close,volume\n2024-01-02,10.0,11.0,9.5,10.5,1000\n2024-01-03,10.5,11.5,10.0,11.0,2000\n")
    (root / "prices" / "T.HK" / "tencent.json").write_text('{\n  "source": "Tencent quote API T.HK (unadjusted)",\n  "fetched_from": "2024-01-02"\n}\n')
    (root / "prices" / "T.HK" / "yahoo.csv").write_text("date,open,high,low,close,adjclose,volume\n2024-01-02,9.5,10.5,9.0,10.0,9.9,1000\n2024-01-03,10.0,11.0,9.5,10.5,10.4,\n")
    (root / "prices" / "T.HK" / "yahoo.json").write_text('{\n  "source": "Yahoo Finance T.HK",\n  "fetched_from": "2024-01-02",\n  "splits": {\n    "2024-01-03": 5.0\n  }\n}\n')
    (root / "prices" / "T.HK" / "final.csv").write_text(
        "date,close,source,status,tencent_close,yahoo_close_unadjusted,yahoo_factor\n"
        "2024-01-02,10.5,tencent,agree,10.5,10.5,0.952381\n2024-01-03,11.0,yahoo,yahoo_only,,11.0,1.0\n"
    )
    (root / "fx" / "CNYHKD.csv").write_text("date,rate\n2024-01-02,1.1\n2024-01-03,1.123456\n")
    (root / "fx" / "CNYHKD.json").write_text('{\n  "source": "Yahoo Finance CNYHKD=X",\n  "fetched_from": "2024-01-02"\n}\n')
    (root / "eps" / "T.HK_announcements.csv").write_text(
        "published,period,table,currency,eps,url,provisional,note,parser_version\n"
        "2024-02-01 17:00,2023Q4,cumulative,CNY,1.5;1.2,https://example.com/a.pdf,0,,5\n"
    )
    (root / "eps" / "T.HK_periods.csv").write_text("period,currency,eps,eps_sources\n2023Q4,CNY,0.42,1\n2024Q1,CNY,,0\n")
    (root / "eps" / "T.HK_ttm.csv").write_text("report_date,published,eps_ttm,eps_ttm_formula\n2024-03-31,2024-05-01 17:00,1.5,2023Q2 0.5 + 2023Q3 1.0 = 1.5\n")
    return root


def test_the_legacy_layout_survives_an_import_and_export_byte_for_byte(tmp_path):
    source = legacy_folder(tmp_path / "old")
    db = Database(tmp_path / "v.db")
    assert import_from(db, source) > 0
    exported = tmp_path / "new"
    export_to(db, exported)
    original = sorted(p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file())
    assert sorted(p.relative_to(exported).as_posix() for p in exported.rglob("*") if p.is_file()) == original
    for rel in original:
        assert (exported / rel).read_bytes() == (source / rel).read_bytes(), rel


def test_importing_twice_changes_nothing(tmp_path):
    source = legacy_folder(tmp_path / "old")
    db = Database(tmp_path / "v.db")
    import_from(db, source)
    before = sha(tmp_path / "v.db")
    import_from(db, source)
    assert sha(tmp_path / "v.db") == before


def test_export_files_for_one_symbol_includes_all_fx_pairs(tmp_path):
    db = Database(tmp_path / "v.db")
    import_from(db, legacy_folder(tmp_path / "old"))
    names = set(export_files(db, "T.HK"))
    assert {"prices/T.HK/final.csv", "eps/T.HK_ttm.csv", "fx/CNYHKD.csv", "fx/CNYHKD.json", "prices/T.HK/yahoo.json"} <= names
    assert export_files(db, "NOBODY").keys() == {"fx/CNYHKD.csv", "fx/CNYHKD.json"}
