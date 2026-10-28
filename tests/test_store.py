from datetime import datetime

import pandas as pd
import pytest

from valuation_monitor.filings import Announcement
from valuation_monitor.providers.hkex import Filing
from valuation_monitor.providers.yahoo import PriceHistory
from valuation_monitor.store import EarningsStore, PriceStore

TODAY = pd.Timestamp.now(tz="Asia/Hong_Kong").tz_localize(None).normalize()


def days_ago(n):
    return TODAY - pd.Timedelta(days=n)


class FakeQuotes:
    currency = "HKD"
    timezone = "Asia/Hong_Kong"

    def __init__(self, closes):
        self.closes = closes  # {日期: 收盘价}
        self.calls = []
        self.offline = False

    def daily_close(self, symbol, start):
        if self.offline:
            raise ConnectionError("offline")
        self.calls.append(pd.Timestamp(start))
        close = pd.Series(self.closes, dtype=float).sort_index()
        return close[close.index >= pd.Timestamp(start)]


class FakeYahoo:
    def __init__(self, splits=None):
        self.splits = splits or {}
        self.offline = False

    def price_history(self, symbol, start):
        if self.offline:
            raise ConnectionError("offline")
        return PriceHistory(pd.Series(dtype=float), pd.Series(self.splits, dtype=float), "HKD", "Asia/Hong_Kong")


def test_price_store_saves_completed_days_and_fetches_incrementally(tmp_path):
    quotes = FakeQuotes({days_ago(30): 100.0, days_ago(2): 101.0, days_ago(1): 102.0, TODAY: 103.0})
    yahoo = FakeYahoo()
    store = PriceStore(quotes, yahoo, tmp_path)

    h = store.prices("0700.HK", days_ago(40).to_pydatetime())
    assert h.close.tolist() == [100.0, 101.0, 102.0, 103.0]  # 含当天实时价
    saved = pd.read_csv(tmp_path / "prices" / "0700.HK.csv", index_col="date", parse_dates=True)["close"]
    assert saved.tolist() == [100.0, 101.0, 102.0]  # 当天未收盘，不保存

    # 第二次只从最后保存日期往前 7 天开始下载；数据源改动历史数据时保留已保存的值
    quotes.closes[days_ago(1)] = 999.0
    h = store.prices("0700.HK", days_ago(40).to_pydatetime())
    assert quotes.calls[-1] == days_ago(8)
    assert h.close[days_ago(1)] == 102.0

    # 调大回看范围时补下载更早的数据
    store.prices("0700.HK", days_ago(60).to_pydatetime())
    assert quotes.calls[-1] == days_ago(60)

    quotes.offline = yahoo.offline = True
    h = store.prices("0700.HK", days_ago(40).to_pydatetime())
    assert h.close.tolist() == [100.0, 101.0, 102.0]


def test_price_store_keeps_raw_prices_and_adjusts_for_splits_in_memory(tmp_path):
    quotes = FakeQuotes({days_ago(10): 500.0, days_ago(5): 510.0, days_ago(1): 103.0})
    yahoo = FakeYahoo({days_ago(3): 5.0})
    store = PriceStore(quotes, yahoo, tmp_path)

    h = store.prices("0700.HK", days_ago(20).to_pydatetime())
    assert h.close.tolist() == pytest.approx([100.0, 102.0, 103.0])
    saved = pd.read_csv(tmp_path / "prices" / "0700.HK.csv", index_col="date", parse_dates=True)["close"]
    assert saved.tolist() == [500.0, 510.0, 103.0]  # 保存的是实际成交价

    yahoo.offline = True  # 拆股记录已保存，离线时照样生效
    h = store.prices("0700.HK", days_ago(20).to_pydatetime())
    assert h.splits.to_dict() == {days_ago(3): 5.0}
    assert h.close.tolist() == pytest.approx([100.0, 102.0, 103.0])


def test_earnings_store_parses_each_filing_once(tmp_path, monkeypatch):
    calls = []

    def fake_parse(filing):
        calls.append(filing.url)
        return Announcement(
            period=pd.Period("2026Q2", freq="Q"),
            published=filing.published,
            url=filing.url,
            currency="CNY",
            is_quarterly_table=True,
            values={"eps": [6.207, 6.115, 6.431], "non_ifrs_eps": [7.581, 6.931, 7.517]},
        )

    monkeypatch.setattr("valuation_monitor.store.parse_announcement", fake_parse)
    filing = Filing("t", pd.Timestamp("2026-08-12 16:31"), "https://example.com/a.pdf", b"")
    store = EarningsStore(tmp_path)

    first = store.load("0700.HK", [filing], pd.Series(dtype=float))
    second = store.load("0700.HK", [filing], pd.Series(dtype=float))
    assert calls == ["https://example.com/a.pdf"]
    assert second.quarters["eps"][pd.Period("2026Q2")].value == first.quarters["eps"][pd.Period("2026Q2")].value
    assert (tmp_path / "eps" / "0700.HK_quarterly.csv").exists()
    assert (tmp_path / "eps" / "0700.HK_ttm.csv").exists()
