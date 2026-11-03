"""The MCP server: read-only, offline, sourced, point in time, and no calculation outside Python."""

import hashlib
import socket
import sqlite3

import pandas as pd
import pytest

from valuation_monitor.analysis import evaluate
from valuation_monitor.audit import explain
from valuation_monitor.config import parse_config
from valuation_monitor.data import StockData
from valuation_monitor.db import Database
from valuation_monitor.filings import Announcement, build_history
from valuation_monitor.mcp_server import RequestError, ValuationService, create_server, open_service
from valuation_monitor.metrics import METRICS
from valuation_monitor.service import Monitor

TODAY = pd.Timestamp("2025-12-31").date()


def announcement(period, published, eps, revenue, profit, equity):
    return Announcement(
        pd.Period(period, freq="Q"), pd.Timestamp(published), f"https://filings.example/{period}.pdf", "CNY", True,
        {"eps": eps, "revenue": revenue, "profit": profit, "equity": equity},
    )


ANNOUNCEMENTS = [
    announcement("2024Q1", "2024-05-01 17:00", [1.0, 0.5, 0.9], [100.0, 90.0, 95.0], [10.0, 5.0, 9.0], [500.0, 480.0]),
    announcement("2024Q2", "2024-08-01 17:00", [2.0, 1.5, 1.0], [110.0, 95.0, 100.0], [20.0, 15.0, 10.0], [520.0, 480.0]),
    announcement("2024Q3", "2024-11-01 17:00", [3.0, 2.5, 2.0], [120.0, 100.0, 110.0], [30.0, 25.0, 20.0], [540.0, 480.0]),
    announcement("2024Q4", "2025-02-03 17:00", [4.0, 3.5, 3.0], [130.0, 105.0, 120.0], [40.0, 35.0, 30.0], [560.0, 480.0]),
    announcement("2025Q1", "2025-05-02 17:00", [5.0, 4.0, 4.0], [140.0, 110.0, 130.0], [50.0, 40.0, 40.0], [580.0, 560.0]),
    announcement("2025Q2", "2025-08-01 17:00", [6.0, 5.0, 5.0], [150.0, 115.0, 140.0], [60.0, 50.0, 50.0], [600.0, 560.0]),
]
DAYS = pd.bdate_range("2024-01-01", "2025-12-31")
CLOSE = pd.Series([100.0 + (i * 7) % 40 for i in range(len(DAYS))], index=DAYS)


def make_data(config, announcements=ANNOUNCEMENTS):
    earnings = build_history(list(announcements))
    return StockData(config.stocks[0], CLOSE, "HKD", pd.Series(1.1, index=DAYS), earnings.ttm, earnings)


@pytest.fixture
def config():
    return parse_config({"stocks": [{"symbol": "0700.HK", "name": "Tencent Holdings", "metrics": ["pe", "pb"]}], "lookback_options": [2, 1]})


def build_service(tmp_path, config, announcements=ANNOUNCEMENTS) -> ValuationService:
    writer = Database(tmp_path / "valuation.db")  # creates the file the way the update job does
    final = pd.DataFrame(
        {"close": CLOSE, "source": "tencent", "status": "agree", "tencent_close": CLOSE, "yahoo_close_unadjusted": CLOSE, "yahoo_factor": 1.0}, index=CLOSE.index
    )
    final.index.name = "date"
    writer.write_series("prices_final", {"symbol": "0700.HK"}, final)
    monitor = Monitor(config, tmp_path, tmp_path / "cache", offline=True, readonly=True)
    data = make_data(config, announcements)
    monitor.loader.load = lambda stock: data
    return ValuationService(monitor, today=TODAY)


@pytest.fixture
def service(tmp_path, config):
    return build_service(tmp_path, config)


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- read only ------------------------------------------------------------------------------------------------------------------


def test_the_database_is_opened_read_only(service):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        service.monitor.db.write_meta("anything", {"a": 1})
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with service.monitor.db.connect() as conn:
            conn.execute("DELETE FROM prices_final")


def test_a_writable_database_is_refused(tmp_path, config):
    monitor = Monitor(config, tmp_path, tmp_path / "cache", offline=True)
    with pytest.raises(ValueError, match="read-only"):
        ValuationService(monitor)


def test_a_missing_database_is_not_created(tmp_path, config):
    with pytest.raises(FileNotFoundError):
        Database(tmp_path / "new.db", readonly=True)
    assert not (tmp_path / "new.db").exists()
    with pytest.raises(FileNotFoundError):
        open_service("config.yaml", str(tmp_path))
    assert not (tmp_path / "valuation.db").exists()


def test_tools_leave_the_files_untouched(tmp_path, service):
    before = {p: file_hash(p) for p in tmp_path.rglob("*") if p.is_file()}
    service.list_stocks()
    service.valuation("0700.HK", "pe")
    service.valuation("0700.HK", "pb", 1, "2025-03-03")
    service.history("0700.HK", "pe")
    service.filings("0700.HK")
    assert {p: file_hash(p) for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not (tmp_path / "cache").exists()


# ---- offline --------------------------------------------------------------------------------------------------------------------


def test_tools_never_touch_the_network(monkeypatch, service):
    def refuse(*args, **kwargs):
        raise AssertionError("a tool call tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    service.list_stocks()
    service.valuation("0700.HK", "pe")
    service.history("0700.HK", "pb")
    service.filings("0700.HK")


# ---- calculation stays in Python and matches the web page ------------------------------------------------------------------------


def test_numbers_are_the_ones_the_web_page_computes(service, config):
    data = make_data(config)
    series = METRICS["pe"].compute(data)
    expected = evaluate(series, 2, config.levels)
    result = service.valuation("0700.HK", "pe", 2)
    assert result["value"] == pytest.approx(expected.current, abs=1e-4)
    assert result["percentile"] == pytest.approx(expected.percentile, abs=0.05)
    assert result["level"] == expected.level
    assert result["window"]["trading_days"] == expected.observations
    assert result["history_statistics"]["max"] == pytest.approx(expected.max, abs=1e-4)
    assert result["value_day"] == f"{expected.as_of:%Y-%m-%d}"


def test_the_ratio_line_adds_up(service):
    result = service.valuation("0700.HK", "pe", 2)
    ratio = result["calculation"]["ratio"]
    close = float(CLOSE.iloc[-1])
    assert ratio.startswith(f"PE (TTM) = close / (per-share value x FX rate) = {close:,.0f} HKD / (")
    assert ratio.endswith(f"= {result['value']:,.4f}".rstrip("0").rstrip("."))


# ---- sources ---------------------------------------------------------------------------------------------------------------------


def test_every_result_names_its_sources(service, config):
    result = service.valuation("0700.HK", "pe", 2)
    record = result["sources"]["earnings_record"]
    assert record["period"] == "2025Q2"
    assert record["published"] == "2025-08-01 17:00"
    assert record["published_timezone"] == "Asia/Hong_Kong"
    assert record["effective_trading_day"] == "2025-08-04"  # published Friday after the close: first used on Monday
    assert record["filing_url"] == "https://filings.example/2025Q2.pdf"
    rec = make_data(config).earnings.ttm_detail[-1]
    assert result["calculation"]["per_share_value"] == explain("eps_ttm", rec)  # the audit.py text, unchanged
    components = result["sources"]["components"]
    assert [c["period"] for c in components] == ["2024Q3", "2024Q4", "2025Q1", "2025Q2"]
    assert all(c["filing_url"].startswith("https://filings.example/") and c["first_published"] and c["unit"] for c in components)
    assert result["sources"]["price"] == {"day": "2025-12-31", "close": float(CLOSE.iloc[-1]), "currency": "HKD", "source": "tencent", "cross_check": "agree"}
    assert result["sources"]["fx_rate"]["rate"] == 1.1


def test_book_value_lists_equity_and_shares_sources(service):
    result = service.valuation("0700.HK", "pb", 1)
    items = [c["item"] for c in result["sources"]["components"]]
    assert items[0].startswith("Equity attributable to owners") and any("share count" in i for i in items)
    assert result["calculation"]["per_share_value"][0].startswith("Book value per share = equity at period end 600")


def test_filings_list_has_links_and_dates(service):
    result = service.filings("0700.HK", limit=3)
    assert [f["period"] for f in result["filings"]] == ["2025Q2", "2025Q1", "2024Q4"]
    assert result["filings"][0]["filing_url"] == "https://filings.example/2025Q2.pdf"
    assert result["filings"][0]["effective_trading_day"] == "2025-08-04"


# ---- point in time ---------------------------------------------------------------------------------------------------------------


def test_as_of_picks_the_filing_in_effect_that_day(service):
    before = service.valuation("0700.HK", "pe", 1, "2025-05-02")  # published that evening, not yet usable
    after = service.valuation("0700.HK", "pe", 1, "2025-05-05")  # the next trading day
    assert before["sources"]["earnings_record"]["period"] == "2024Q4"
    assert after["sources"]["earnings_record"]["period"] == "2025Q1"
    assert before["as_of"] == "2025-05-02" and before["value_day"] == "2025-05-02"


def test_as_of_gives_the_same_answer_as_data_that_stops_there(tmp_path, config):
    """The strong form of "no future data": remove everything published after the day and the answer must not change."""
    full = build_service(tmp_path / "full", config)
    known = [a for a in ANNOUNCEMENTS if a.published <= pd.Timestamp("2025-03-03 23:59")]
    cut = build_service(tmp_path / "cut", config, known)
    for metric in ("pe", "pb"):
        a = full.valuation("0700.HK", metric, 1, "2025-03-03")
        b = cut.valuation("0700.HK", metric, 1, "2025-03-03")
        for key in ("value", "percentile", "level", "window", "history_statistics", "calculation", "sources"):
            assert a[key] == b[key], (metric, key)


def test_nothing_after_as_of_is_returned(service):
    result = service.history("0700.HK", "pe", 2, "2025-03-03", points=240)
    assert result["window"]["end"] <= "2025-03-03" and all(v["date"] <= "2025-03-03" for v in result["values"])
    assert all(f["effective_trading_day"] <= "2025-03-03" for f in service.filings("0700.HK", "2025-03-03")["filings"])


def test_before_the_first_filing_is_an_error_not_a_guess(service):
    with pytest.raises(RequestError, match="no earnings were published yet"):
        service.valuation("0700.HK", "pe", 1, "2024-03-01")


def test_as_of_after_the_data_uses_the_newest_day_and_says_so(service):
    result = service.valuation("0700.HK", "pe", 1, "2030-01-01")
    assert result["as_of"] == "2025-12-31" and any("after the newest saved price" in w for w in result["warnings"])


def test_a_short_history_is_reported(service):
    result = service.valuation("0700.HK", "pe", 2, "2025-03-03")
    assert result["window"]["covers_years_asked"] is False
    assert any("years of history exist" in w for w in result["warnings"])


# ---- requests that cannot be answered ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, message",
    [
        (("nope", "pe"), "Unknown symbol"),
        (("0700.HK", "ps"), "has no metric"),
        (("0700.HK", "pe", 99), "years must be between"),
        (("0700.HK", "pe", 1, "03/03/2025"), "YYYY-MM-DD"),
    ],
)
def test_bad_requests_say_what_is_wrong(service, args, message):
    with pytest.raises(RequestError, match=message):
        service.valuation(*args)


def test_list_stocks_shows_coverage_and_levels(service):
    result = service.list_stocks()
    stock = result["stocks"][0]
    assert stock["symbol"] == "0700.HK" and stock["metrics"] == {"pe": "PE (TTM)", "pb": "PB"}
    assert stock["latest_price_date"] == "2025-12-31" and stock["latest_price_age_days"] == 0 and stock["filings_saved"] == 6
    assert [lv["label"] for lv in result["levels"]][0] == "Deeply undervalued"


# ---- the MCP wrapper -----------------------------------------------------------------------------------------------------------------


def test_the_tools_are_declared_read_only(service):
    import asyncio

    server = create_server(service)
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"list_stocks", "get_valuation", "get_history", "get_filings"}
    for tool in tools:
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False and tool.annotations.openWorldHint is False
        assert tool.description


def test_a_tool_call_through_the_protocol_returns_the_sources(service):
    import asyncio
    import json

    server = create_server(service)
    content = asyncio.run(server.call_tool("get_valuation", {"symbol": "0700.HK", "metric": "pe", "years": 1, "as_of": "2025-05-05"}))
    text = content[0].text if isinstance(content, (list, tuple)) and hasattr(content[0], "text") else json.dumps(content)
    assert "https://filings.example/2025Q1.pdf" in text and "2025-05-05" in text
