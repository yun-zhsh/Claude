import pandas as pd
import pytest

from valuation_monitor.analysis import classify, evaluate
from valuation_monitor.config import DEFAULT_LEVELS, StockConfig, parse_config
from valuation_monitor.data import StockData, add_available_date, adjust_for_splits
from valuation_monitor.metrics import METRICS
from valuation_monitor.providers.eastmoney import to_secucode

STOCK = StockConfig(symbol="0700.HK", name="腾讯控股", report_currency="CNY")


def make_data(close, fx, fundamentals):
    idx = pd.to_datetime(list(close))
    return StockData(
        stock=STOCK,
        close=pd.Series(list(close.values()), index=idx, dtype=float),
        price_currency="HKD",
        fx=pd.Series(fx, index=idx, dtype=float),
        fundamentals=fundamentals,
    )


def fundamentals(rows):
    df = pd.DataFrame(rows, columns=["report_date", "is_annual", "eps_ttm"])
    df["report_date"] = pd.to_datetime(df["report_date"])
    return add_available_date(df, annual_lag_days=90, interim_lag_days=60)


def test_pe_uses_only_published_eps_and_converts_currency():
    f = fundamentals([("2023-12-31", True, 10.0), ("2024-03-31", False, 20.0)])
    data = make_data(
        {"2024-03-01": 100.0, "2024-03-30": 100.0, "2024-05-30": 100.0, "2024-05-31": 100.0},
        fx=1.1,
        fundamentals=f,
    )
    pe = METRICS["pe"].compute(data)
    # 2024-03-01：年报尚未公布（12-31 + 90 天 = 03-30），没有可用 EPS
    assert pd.isna(pe.iloc[0])
    assert pe.iloc[1] == pytest.approx(100 / (10 * 1.1))
    # 一季报在 03-31 + 60 天 = 05-30 才可用
    assert pe.iloc[2] == pytest.approx(100 / (20 * 1.1))
    assert pe.iloc[3] == pytest.approx(100 / (20 * 1.1))


def test_pe_is_nan_when_eps_not_positive():
    f = fundamentals([("2023-12-31", True, -5.0)])
    data = make_data({"2024-06-01": 100.0}, fx=1.0, fundamentals=f)
    assert pd.isna(METRICS["pe"].compute(data).iloc[0])


def test_adjust_for_splits_only_affects_earlier_reports():
    f = fundamentals([("2014-03-31", False, 10.0), ("2014-06-30", False, 2.2)])
    splits = pd.Series([5.0], index=pd.to_datetime(["2014-05-15"]))
    out = adjust_for_splits(f, splits, ["eps_ttm"])
    assert out["eps_ttm"].tolist() == pytest.approx([2.0, 2.2])


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


def test_to_secucode():
    assert to_secucode("0700.HK") == "00700.HK"
    with pytest.raises(ValueError):
        to_secucode("AAPL")


def test_parse_config_rejects_unknown_metric():
    with pytest.raises(ValueError, match="未知指标"):
        parse_config({"stocks": [{"symbol": "0700.HK", "report_currency": "CNY", "metrics": ["xyz"]}]})
