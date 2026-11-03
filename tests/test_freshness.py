"""A price or FX source that silently stops delivering must make the update job fail; a quiet company only gets a warning."""

import pandas as pd
import pytest

from valuation_monitor import cli
from valuation_monitor.config import DEFAULT_MAX_PRICE_AGE_DAYS, parse_config
from valuation_monitor.db import FINAL_COLUMNS, Database
from valuation_monitor.freshness import check, report

NOW = pd.Timestamp("2026-11-10")


def config(**freshness):
    stocks = [
        {"symbol": "0700.HK", "name": "Tencent Holdings"},
        {"symbol": "005930.KS", "name": "Samsung Electronics", "metrics": ["pe"]},
        {"symbol": "002415.SZ", "name": "Hikvision", "metrics": ["pe"]},
    ]
    return parse_config({"stocks": stocks, "freshness": freshness})


def save_final(db, symbol, last_day):
    days = pd.date_range(end=last_day, periods=3, name="date")
    frame = pd.DataFrame({"close": 1.0, "source": "yahoo", "status": "yahoo_only", "tencent_close": None, "yahoo_close_unadjusted": 1.0, "yahoo_factor": 1.0}, index=days)
    db.write_series("prices_final", {"symbol": symbol}, frame[FINAL_COLUMNS], replace=True)  # replace: the series ends on last_day


def save_fx(db, pair, last_day):
    db.write_series("fx_rates", {"pair": pair}, pd.DataFrame({"rate": [1.1, 1.2]}, index=pd.date_range(end=last_day, periods=2, name="date")), replace=True)
    db.write_meta(f"fx/{pair}", {"source": "test"})


def save_filing(db, symbol, published):
    db.write_frame("announcements", symbol, pd.DataFrame({"published": [published], "period": ["2026Q2"]}), text=True)


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "v.db")
    for symbol in ("0700.HK", "005930.KS", "002415.SZ"):
        save_final(db, symbol, "2026-11-09")
        save_filing(db, symbol, "2026-10-30 17:00")
    save_fx(db, "CNYHKD", "2026-11-09")
    return db


def by_subject(findings):
    return {f.subject: f for f in findings}


def test_fresh_data_passes(db, capsys):
    findings = check(db, config(), NOW)
    assert not any(f.stale for f in findings) and report(findings) is False
    assert "OK" in capsys.readouterr().out


def test_a_price_series_older_than_its_market_limit_is_stale(db, capsys):
    save_final(db, "005930.KS", "2026-10-30")  # 11 days old; the limit for Korea is 6
    findings = by_subject(check(db, config(), NOW))
    assert findings["005930.KS prices"].stale and findings["005930.KS prices"].age_days == 11
    assert not findings["0700.HK prices"].stale
    assert report(list(findings.values())) is True
    assert "STALE 005930.KS prices: newest 2026-10-30, 11 days old (limit 6)" in capsys.readouterr().err


def test_the_limit_depends_on_the_market(db):
    # nine days old: stale for Hong Kong (5) and Korea (6), still fine for China (10: Golden Week)
    for symbol in ("0700.HK", "005930.KS", "002415.SZ"):
        save_final(db, symbol, "2026-11-01")
    stale = {f.subject for f in check(db, config(), NOW) if f.stale}
    assert stale == {"0700.HK prices", "005930.KS prices"}


def test_the_limits_come_from_the_config(db):
    save_final(db, "0700.HK", "2026-11-01")
    assert any(f.stale for f in check(db, config(), NOW))
    relaxed = config(max_price_age_days={"HK": 12})
    assert not any(f.stale and f.kind == "price" for f in check(db, relaxed, NOW))


def test_a_stock_without_any_saved_price_is_stale(tmp_path):
    findings = by_subject(check(Database(tmp_path / "empty.db"), config(), NOW))
    assert findings["0700.HK prices"].stale and findings["0700.HK prices"].last is None
    assert "nothing is saved" in findings["0700.HK prices"].line()


def test_a_stale_fx_rate_is_blocking_too(db):
    save_fx(db, "CNYUSD", "2026-10-25")  # 16 days old; the limit is 7
    findings = by_subject(check(db, config(), NOW))
    assert findings["CNYUSD FX rate"].stale and findings["CNYUSD FX rate"].blocking
    assert report(list(findings.values())) is True


def test_an_old_filing_is_only_a_warning(db, capsys):
    save_filing(db, "0700.HK", "2026-01-15 17:00")  # 299 days ago
    findings = check(db, config(), NOW)
    old = by_subject(findings)["0700.HK filings"]
    assert old.stale and not old.blocking and old.status.strip() == "WARN"
    assert report(findings) is False  # does not fail the job
    assert "WARN  0700.HK filings" in capsys.readouterr().err


def test_freshness_limits_are_validated():
    with pytest.raises(ValueError, match="unknown market"):
        config(max_price_age_days={"JP": 5})
    with pytest.raises(ValueError, match="positive"):
        config(max_fx_age_days=0)
    assert config().freshness.max_price_age_days == DEFAULT_MAX_PRICE_AGE_DAYS


def test_check_command_exit_codes(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda path: config())
    assert cli.check_main(["--db", str(tmp_path / "missing.db")]) == 2
    monkeypatch.setattr("valuation_monitor.freshness.today", lambda: NOW)
    assert cli.check_main(["--db", str(db.path)]) == 0
    save_final(db, "0700.HK", "2026-10-01")
    assert cli.check_main(["--db", str(db.path)]) == 1
    assert "STALE 0700.HK prices" in capsys.readouterr().err


def test_update_job_saves_everything_and_then_fails_on_stale_data(monitor, tmp_path, monkeypatch, capsys):
    """The failure comes last: the data was saved (and would have been uploaded) before the job reports it."""
    monkeypatch.delenv("VALUATION_DB_URI", raising=False)
    monkeypatch.setattr(cli, "Monitor", lambda *a, **k: monitor)
    monkeypatch.setattr(cli, "load_config", lambda path: monitor.config)
    # the web fixture's database holds a price from 2024: stale for today's date
    assert cli.update_main(["--db", str(tmp_path / "v.db")]) == 1
    captured = capsys.readouterr()
    assert "Tencent Holdings (0700.HK): updated" in captured.out  # the stock was updated before the verdict
    assert "STALE 0700.HK prices" in captured.err and "some price or FX data is stale" in captured.err
