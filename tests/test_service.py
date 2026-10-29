from types import SimpleNamespace

import pandas as pd
import pytest

from valuation_monitor.config import parse_config
from valuation_monitor.data import StockData
from valuation_monitor.service import Monitor


def make_monitor(tmp_path, closes):
    config = parse_config({"stocks": [{"symbol": "0700.HK", "name": "腾讯控股", "metrics": ["pe"]}]})
    monitor = Monitor(config, data_dir=tmp_path, cache_dir=tmp_path / "cache")
    days = pd.bdate_range("2023-01-02", periods=len(closes))
    fundamentals = pd.DataFrame(
        {"available_date": [days[0] - pd.Timedelta(days=1)], "eps_ttm": [1.0], "non_ifrs_eps_ttm": [1.0]}
    )
    data = StockData(
        config.stocks[0], pd.Series(closes, index=days, dtype=float), "HKD", pd.Series(1.0, index=days), fundamentals,
        SimpleNamespace(discrepancies=lambda: []),
    )
    monitor.loader.load = lambda stock: data
    monitor.price_check = lambda symbol: {}
    return monitor


def test_each_lookback_window_is_evaluated_separately(tmp_path):
    # 前 600 个交易日在 200，最近 300 个交易日（超过一年）在 100：当前值 PE = 100
    monitor = make_monitor(tmp_path, [200.0] * 600 + [100.0] * 300)
    result = monitor.analyze(monitor.config.stocks[0]).metrics[0]

    assert list(result.windows) == [10, 5, 3, 2, 1]
    assert result.default_years == 10 and result.valuation is result.windows[10].valuation
    # 最近一年里 PE 一直是 100：当前值就是区间内最高的，分位 100%，属于“极度高估”（最后一档）
    assert result.windows[1].valuation.percentile == 100
    assert result.windows[1].level_index == len(monitor.config.levels) - 1
    # 三年窗口里有一半多的日子在 200，当前值排在后面 38% 左右，属于“合理”档
    assert result.windows[3].valuation.percentile == pytest.approx(300 / 783 * 100, abs=1.5)
    assert result.windows[3].level_index == 2
    # 窗口越短，区间越靠后
    starts = [result.windows[y].valuation.window_start for y in (10, 5, 3, 2, 1)]
    assert starts == sorted(starts)
    assert result.windows[1].valuation.observations < result.windows[3].valuation.observations


def test_data_window_follows_the_longest_option(tmp_path):
    monitor = make_monitor(tmp_path, [100.0] * 10)
    assert monitor.config.max_lookback == 10
