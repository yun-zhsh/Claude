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


def test_ps_and_pb_use_per_share_values_in_price_currency():
    idx = pd.to_datetime(["2024-06-03", "2024-06-04"])
    f = pd.DataFrame(
        {
            "available_date": pd.to_datetime(["2024-06-01", "2024-06-04"]),
            "revenue_per_share_ttm": [50.0, 60.0],
            "book_value_per_share": [100.0, -5.0],
        }
    )
    data = StockData(STOCK, pd.Series([110.0, 110.0], index=idx), "HKD", pd.Series(1.1, index=idx), f)
    ps = METRICS["ps"].compute(data)
    assert ps.tolist() == pytest.approx([110 / (50 * 1.1), 110 / (60 * 1.1)])
    pb = METRICS["pb"].compute(data)
    assert pb.iloc[0] == pytest.approx(110 / (100 * 1.1))
    assert pd.isna(pb.iloc[1])  # 净资产为负时 PB 无意义


def test_parse_config_filing_format():
    raw = {"stocks": [{"symbol": "2382.HK", "name": "舜宇光学科技", "filing_format": "statements", "metrics": ["pe"]}]}
    assert parse_config(raw).stocks[0].filing_format == "statements"
    assert parse_config({"stocks": [{"symbol": "0700.HK"}]}).stocks[0].filing_format == "highlights"
    with pytest.raises(ValueError, match="公告版式"):
        parse_config({"stocks": [{"symbol": "0700.HK", "filing_format": "xyz"}]})


def test_parse_config_lookback_options():
    raw = {"stocks": [{"symbol": "0700.HK"}]}
    c = parse_config(raw)
    assert c.lookback_options == [10, 5, 3, 2, 1] and c.lookback_years == 10 and c.max_lookback == 10
    c = parse_config({**raw, "lookback_options": [1, 3, 3], "lookback_years": 3})
    assert c.lookback_options == [3, 1] and c.lookback_years == 3 and c.max_lookback == 3
    with pytest.raises(ValueError, match="lookback_years"):
        parse_config({**raw, "lookback_options": [5, 1], "lookback_years": 10})  # 默认值必须是可选年数之一
    with pytest.raises(ValueError, match="正整数"):
        parse_config({**raw, "lookback_options": [0, 3], "lookback_years": 3})


def test_parse_config_us_stock():
    raw = {"stocks": [{"symbol": "JD", "name": "京东（ADR）", "cik": 1549802, "metrics": ["pe", "pe_non_gaap"]}]}
    jd = parse_config(raw).stocks[0]
    assert (jd.market, jd.filing_format, jd.cik) == ("US", "press_release", 1549802)
    assert jd.filing_source == "美国证监会 EDGAR" and not jd.cross_checks_prices
    hk = parse_config({"stocks": [{"symbol": "0700.HK"}]}).stocks[0]
    assert (hk.market, hk.cik, hk.filing_source, hk.cross_checks_prices) == ("HK", None, "港交所披露易", True)
    with pytest.raises(ValueError, match="cik"):
        parse_config({"stocks": [{"symbol": "JD"}]})
    with pytest.raises(ValueError, match="不匹配"):
        parse_config({"stocks": [{"symbol": "JD", "cik": 1, "filing_format": "highlights"}]})
    with pytest.raises(ValueError, match="不匹配"):
        parse_config({"stocks": [{"symbol": "0700.HK", "filing_format": "press_release"}]})
    with pytest.raises(ValueError, match="未知的市场"):
        parse_config({"stocks": [{"symbol": "X", "market": "JP"}]})


def test_non_gaap_pe_uses_non_gaap_eps():
    data = make_data({"2024-06-01": 100.0}, fx=2.0, ttm_rows=[("2024-01-01", 10.0, 20.0)])
    assert METRICS["pe_non_gaap"].compute(data).iloc[0] == pytest.approx(100 / (20 * 2.0))


def test_parse_config_korean_stock():
    kr = parse_config({"stocks": [{"symbol": "005930.KS", "name": "三星电子", "metrics": ["pe"]}]}).stocks[0]
    assert (kr.market, kr.filing_format, kr.cik) == ("KR", "kifrs_statements", None)
    assert not kr.cross_checks_prices and "三星" in kr.filing_source
    with pytest.raises(ValueError, match="不匹配"):
        parse_config({"stocks": [{"symbol": "005930.KS", "filing_format": "highlights"}]})
