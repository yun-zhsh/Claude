import pandas as pd
import pytest

from valuation_monitor.filings import (
    Announcement,
    adjust_for_splits,
    announcements_from_frame,
    announcements_to_frame,
    available_date,
    build_history,
    parse_equity,
    parse_summary_table,
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
def test_parse_summary_table(text, eps, non_ifrs):
    currency, values = parse_summary_table(text)
    assert currency == "CNY"
    assert values["eps"] == eps and values["non_ifrs_eps"] == non_ifrs


def test_parse_summary_table_requires_eps_row():
    with pytest.raises(ValueError):
        parse_summary_table("Revenues 100 90 11%")


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
        published=pd.Timestamp(available) - pd.Timedelta(hours=7),  # 前一天 17:00 发布
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


def test_announcements_round_trip_through_frame():
    original = [
        ann("2015Q2", "2015-08-13", False, [1.528, 1.332], [1.634, 1.22]),
        ann("2016Q1", "2016-05-19", True, [0.981, 0.741, 0.769]),
    ]
    restored = announcements_from_frame(announcements_to_frame(original).astype(str))
    assert restored == original


def test_adjust_for_splits_uses_publish_time():
    a = ann("2014Q1", "2014-05-15", True, [3.5, 2.204, 2.125])  # 2014-05-14 17:00 发布，拆股前
    b = ann("2014Q2", "2014-08-14", False, [1.332, 0.843])
    splits = pd.Series([5.0], index=pd.to_datetime(["2014-05-15"]))
    adjusted = adjust_for_splits([a, b], splits)
    assert adjusted[0].values["eps"] == pytest.approx([0.7, 0.4408, 0.425])
    assert adjusted[1].values == b.values
    assert a.values["eps"] == [3.5, 2.204, 2.125]  # 原对象不变


SUMMARY_2026Q2 = """(RMB in millions, unless specified) Revenues 204,785 184,504 11% 196,458 4% Gross profit 118,433
105,013 13% 111,265 6% Operating profit 67,276 60,104 12% 67,375 -0.1% Profit for the period 57,977 56,044 3% 59,392 -2%
Profit attributable to equity holders of the Company 56,022 55,628 0.7% 58,093 -4% EPS (RMB per share) – basic 6.207
6.115 2% 6.431 -3% – diluted 6.104 5.996 2% 6.302 -3% Non-IFRS profit attributable to equity holders of the Company
68,415 63,052 9% 67,905 0.8% Non-IFRS EPS (RMB per share) – basic 7.581 6.931 9% 7.517 0.9% – diluted 7.433"""

SUMMARY_2015H1 = """Revenues 45,828 38,146 20% Gross profit 27,433 Profit attributable to equity holders of the Company *
14,197 12,293 15% EPS (RMB per share) - basic 1.528 1.332 15% - diluted 1.512 1.313 15%"""

BALANCE_SHEET = """EQUITY Equity attributable to equity holders of the Company Share capital – – Share premium 73,692 63,796
Treasury shares (2,180) (3,450) Shares held for share award scheme (8,315) (7,124) Other reserves 5,893 90,494
Retained earnings 1,066,763 1,010,436 1,135,853 1,154,152 Non-controlling interests 87,000 86,913"""


def test_parse_summary_table_amounts():
    _, values = parse_summary_table(SUMMARY_2026Q2)
    assert values["revenue"] == [204785, 184504, 196458]
    assert values["profit"] == [56022, 55628, 58093]  # 不会取到 Non-IFRS 那一行
    _, values = parse_summary_table(SUMMARY_2015H1)
    assert values["revenue"] == [45828, 38146]
    assert values["profit"] == [14197, 12293]  # 跳过脚注标记


def test_parse_equity():
    assert parse_equity(BALANCE_SHEET) == [1135853, 1154152]
    assert parse_equity("no balance sheet here") is None


def test_build_history_revenue_shares_and_book_value():
    def full(period, available, eps, revenue, profit, equity):
        a = ann(period, available, True, eps)
        a.values |= {"revenue": revenue, "profit": profit, "equity": equity}
        return a

    history = build_history(
        [
            full("2025Q2", "2025-08-14", [6.115, 5.112, 5.252], [184504, 161117, 180022], [55628, 47630, 47821], [1114639, 973548]),
            full("2025Q3", "2025-11-14", [6.952, 5.762, 6.115], [192869, 167193, 184504], [63133, 53230, 55628], [1172080, 973548]),
            full("2025Q4", "2026-03-19", [6.433, 5.597, 6.952], [194371, 172446, 192869], [58260, 51324, 63133], [1154152, 973548]),
        ]
    )
    row = history.ttm.iloc[-1]
    assert row["revenue_ttm"] == 180022 + 184504 + 192869 + 194371  # 2025Q1～Q4
    assert row["revenue_per_share_ttm"] == pytest.approx(row["revenue_ttm"] / row["shares"], rel=1e-4)
    assert row["shares"] == pytest.approx(58260 / 6.433, abs=0.1)
    assert row["equity"] == 1154152
    assert row["book_value_per_share"] == pytest.approx(1154152 / (58260 / 6.433), rel=1e-4)
    # 2024 年末净资产在三份公告中都以“上年末”出现，数值一致，不报差异
    assert len(history.quarters["equity"][pd.Period("2024Q4")].observations) == 3
    assert history.discrepancies() == []


def test_adjust_for_splits_leaves_amounts():
    a = ann("2014Q1", "2014-05-15", True, [3.5, 2.204, 2.125])
    a.values["revenue"] = [18400.0, 13550.0, 16970.0]
    adjusted = adjust_for_splits([a], pd.Series([5.0], index=pd.to_datetime(["2014-05-15"])))[0]
    assert adjusted.values["eps"] == pytest.approx([0.7, 0.4408, 0.425])
    assert adjusted.values["revenue"] == [18400.0, 13550.0, 16970.0]
