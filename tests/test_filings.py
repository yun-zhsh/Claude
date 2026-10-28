import pandas as pd
import pytest

from valuation_monitor.filings import (
    Announcement,
    available_date,
    build_history,
    parse_eps_table,
    parse_period,
    split_factor,
)
from valuation_monitor.providers.hkex import stock_code

# 以下文字摘自腾讯业绩公告第一页（pypdf 提取结果）
QUARTERLY_2026Q2 = """Profit attributable to equity
 holders of the Company 56,022 55,628 0.7% 58,093 -4%
EPS (RMB per share)
 – basic 6.207 6.115 2% 6.431 -3%
 – diluted 6.104 5.996 2% 6.302 -3%
Non-IFRS operating profit 75,636 69,248 9% 75,627 stable
Non-IFRS EPS (RMB per share)
 – basic 7.581 6.931 9% 7.517 0.9%
 – diluted 7.433 6.793 9% 7.364 0.9%"""

QUARTERLY_2018Q3 = """rs of the Company 19,710 17,070 15% 19,716 /H11502 EPS (RMB per share) - basic 2.469 1.912 29% 1.893 30%
- diluted 2.440 1.888 29% 1.868 31% Non-GAAP EPS (RMB per share) - basic 2.085 1.812 15% 2.089 /H11502
- diluted 2.061 1.790 15% 2.062 /H11502"""

CUMULATIVE_2015H1 = """equity holders of the Company * 15,177 11,265 35% EPS (RMB per share) - basic 1.528 1.332 15%
- diluted 1.512 1.313 15% Non-GAAP EPS (RMB per share) * - basic 1.634 1.220 34% - diluted 1.616 1.203 34%"""

FOOTNOTED_2014Q3 = """y holders of the Company (1) 17,501 12,535 40% EPS (2) (RMB per share) - basic 1.948 1.264 54%
- diluted 1.918 1.242 54% Non-GAAP EPS (1)(2) (RMB per share) - basic 1.899 1.367 39% - diluted 1.870 1.343 39%"""


@pytest.mark.parametrize(
    "text,eps,non_ifrs",
    [
        (QUARTERLY_2026Q2, [6.207, 6.115, 6.431], [7.581, 6.931, 7.517]),
        (QUARTERLY_2018Q3, [2.469, 1.912, 1.893], [2.085, 1.812, 2.089]),
        (CUMULATIVE_2015H1, [1.528, 1.332], [1.634, 1.220]),
        (FOOTNOTED_2014Q3, [1.948, 1.264], [1.899, 1.367]),
    ],
)
def test_parse_eps_table(text, eps, non_ifrs):
    currency, values = parse_eps_table(text)
    assert currency == "CNY"
    assert values == {"eps": eps, "non_ifrs_eps": non_ifrs}


def test_parse_eps_table_requires_eps_row():
    with pytest.raises(ValueError):
        parse_eps_table("Revenues 100 90 11%")


@pytest.mark.parametrize(
    "title,period",
    [
        ("ANNOUNCEMENT OF THE RESULTS FOR THE THREE MONTHS ENDED 31 MARCH 2026", "2026Q1"),
        ("ANNOUNCEMENT OF THE RESULTS FOR THE THREE AND SIX MONTHS ENDED 30 JUNE 2025", "2025Q2"),
        ("ANNOUNCEMENT OF THE RESULTS FOR THE THREE AND NINE MONTHS ENDED 30 SEPTEMBER 2025", "2025Q3"),
        ("ANNOUNCEMENT OF THE ANNUAL RESULTS FOR THE YEAR ENDED 31 DECEMBER 2025", "2025Q4"),
    ],
)
def test_parse_period(title, period):
    assert parse_period(title) == pd.Period(period, freq="Q")


def test_available_date_after_market_close():
    assert available_date(pd.Timestamp("2026-08-12 16:31")) == pd.Timestamp("2026-08-13")
    assert available_date(pd.Timestamp("2026-08-12 12:00")) == pd.Timestamp("2026-08-12")


def test_split_factor_only_counts_later_splits():
    splits = pd.Series([5.0], index=pd.to_datetime(["2014-05-15"]))
    assert split_factor(pd.Timestamp("2014-05-14 17:10"), splits) == 5.0
    assert split_factor(pd.Timestamp("2014-08-13 17:12"), splits) == 1.0


def ann(period, available, quarterly, eps, non_ifrs=None):
    values = {"eps": eps}
    if non_ifrs is not None:
        values["non_ifrs_eps"] = non_ifrs
    return Announcement(
        period=pd.Period(period, freq="Q"),
        available=pd.Timestamp(available),
        url=f"https://example.com/{period}.pdf",
        currency="CNY",
        is_quarterly_table=quarterly,
        values=values,
    )


def test_build_history_derives_quarters_from_cumulative_and_computes_ttm():
    history = build_history(
        [
            ann("2015Q1", "2015-05-14", True, [0.741, 0.700, 0.632]),  # 本季、去年同季、上季
            ann("2015Q2", "2015-08-13", False, [1.528, 1.332]),  # 上半年累计
            ann("2015Q3", "2015-11-11", False, [2.328, 1.948]),  # 前三季度累计
            ann("2015Q4", "2016-03-18", False, [3.097, 2.579]),  # 全年
            ann("2016Q1", "2016-05-19", True, [0.981, 0.741, 0.769]),
        ]
    )
    q = history.quarters["eps"]
    assert q[pd.Period("2015Q2")].value == pytest.approx(0.787)
    assert q[pd.Period("2015Q3")].value == pytest.approx(0.800)
    assert q[pd.Period("2015Q4")].value == pytest.approx(0.769)
    # 去年的季度：2014Q2 = 2014H1 - 2014Q1，2014Q3 = 2014 前三季度 - 2014H1
    assert q[pd.Period("2014Q2")].value == pytest.approx(0.632)
    assert q[pd.Period("2014Q3")].value == pytest.approx(0.616)
    assert q[pd.Period("2014Q2")].available == pd.Timestamp("2015-08-13")
    # 2015Q4 先由全年减前三季度推算，后被 2016Q1 公告的“上季”直接证实
    assert len(q[pd.Period("2015Q4")].observations) == 2
    assert history.discrepancies() == []

    ttm = history.ttm.set_index("report_date")["eps_ttm"]
    assert ttm[pd.Timestamp("2015-12-31")] == pytest.approx(3.097)
    assert ttm[pd.Timestamp("2016-03-31")] == pytest.approx(0.787 + 0.800 + 0.769 + 0.981)
    assert pd.isna(history.ttm.set_index("report_date")["non_ifrs_eps_ttm"]).all()


def test_build_history_flags_restatement_and_keeps_first_value():
    history = build_history(
        [
            ann("2015Q1", "2015-05-14", True, [0.741, 0.700, 0.632], [0.759, 0.563, 0.725]),
            ann("2016Q1", "2016-05-19", True, [0.981, 0.741, 0.769], [1.072, 0.776, 0.961]),
        ]
    )
    q = history.quarters["non_ifrs_eps"][pd.Period("2015Q1")]
    assert q.value == 0.759
    issues = history.discrepancies()
    assert len(issues) == 1 and "2015Q1" in issues[0]


def test_build_history_rejects_mixed_currency():
    a, b = ann("2015Q1", "2015-05-14", True, [1.0]), ann("2015Q2", "2015-08-13", True, [1.0])
    b.currency = "USD"
    with pytest.raises(ValueError, match="币种"):
        build_history([a, b])


def test_stock_code():
    assert stock_code("0700.HK") == "00700"
    with pytest.raises(ValueError):
        stock_code("AAPL")
