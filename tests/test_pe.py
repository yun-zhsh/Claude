import pandas as pd
import pytest

from valuation_monitor.analysis import classify, evaluate
from valuation_monitor.config import DEFAULT_LEVELS, StockConfig, parse_config
from valuation_monitor.data import StockData
from valuation_monitor.metrics import METRICS

STOCK = StockConfig(symbol="0700.HK", name="腾讯控股")


def make_data(close, fx, ttm_rows):
    idx = pd.to_datetime(list(close))
    f = pd.DataFrame(ttm_rows, columns=["available_date", "eps_ttm", "non_ifrs_eps_ttm"])
    f["available_date"] = pd.to_datetime(f["available_date"])
    return StockData(
        stock=STOCK,
        close=pd.Series(list(close.values()), index=idx, dtype=float),
        price_currency="HKD",
        fx=pd.Series(fx, index=idx, dtype=float),
        fundamentals=f,
    )


def test_pe_uses_only_published_eps_and_converts_currency():
    data = make_data(
        {"2024-03-20": 100.0, "2024-03-21": 100.0, "2024-05-15": 100.0},
        fx=1.1,
        ttm_rows=[("2024-03-21", 10.0, 12.0), ("2024-05-15", 20.0, 25.0)],
    )
    pe = METRICS["pe"].compute(data)
    assert pd.isna(pe.iloc[0])  # 公告尚未发布
    assert pe.iloc[1] == pytest.approx(100 / (10 * 1.1))
    assert pe.iloc[2] == pytest.approx(100 / (20 * 1.1))
    assert METRICS["pe_non_ifrs"].compute(data).iloc[2] == pytest.approx(100 / (25 * 1.1))


def test_pe_is_nan_when_eps_not_positive():
    data = make_data({"2024-06-01": 100.0}, fx=1.0, ttm_rows=[("2024-01-01", -5.0, 1.0)])
    assert pd.isna(METRICS["pe"].compute(data).iloc[0])


def test_evaluate_percentile_and_window():
    idx = pd.date_range("2010-01-01", periods=21, freq="YS")
    series = pd.Series(range(21), index=idx, dtype=float)  # 2010..2030，值 0..20
    v = evaluate(series, lookback_years=9, levels=DEFAULT_LEVELS)
    # 窗口 2021..2030 共 10 个值 11..20，当前值 20 是最高
    assert v.observations == 10
    assert v.current == 20
    assert v.percentile == 100
    assert v.level == "极度高估"


@pytest.mark.parametrize("pct,label", [(0, "极度低估"), (10, "极度低估"), (10.1, "低估"), (50, "合理"), (85, "高估"), (100, "极度高估")])
def test_classify(pct, label):
    assert classify(pct, DEFAULT_LEVELS) == label


def test_parse_config_rejects_unknown_metric():
    with pytest.raises(ValueError, match="未知指标"):
        parse_config({"stocks": [{"symbol": "0700.HK", "metrics": ["xyz"]}]})
