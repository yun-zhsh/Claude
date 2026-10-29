import pandas as pd
import pytest

from valuation_monitor.audit import Component, explain, num, split_notes, sum_formula
from valuation_monitor.filings import Announcement, build_history


def test_num_formats_for_use_inside_sums():
    assert num(1234.5) == "1,234.5" and num(2874.0) == "2,874" and num(0.123456) == "0.1235"
    assert num(-1.93) == "(-1.93)" and num(None) == "—"


def test_sum_formula_marks_derived_periods():
    comps = [Component("2025Q3", 1802.0), Component("2025Q4", 2874.0, derived=True)]
    assert sum_formula(comps, 4676.0) == "2025Q3 1,802 + 2025Q4 2,874* = 4,676"
    assert sum_formula(None, None) == ""


def test_split_adjustment_is_explained():
    assert split_notes([Component("2018Q1", 1708.7, raw=85435.0), Component("2018Q2", 1617.0)]) == [
        "2018Q1 公告原文是 85,435，按拆股记录调整为 1,708.7"
    ]


def history(provisional=False):
    def a(period, published, eps, revenue, profit, equity, **kw):
        return Announcement(pd.Period(period, freq="Q"), pd.Timestamp(published), f"u-{period}", "CNY", True,
                            {"eps": eps, "revenue": revenue, "profit": profit, "equity": equity}, **kw)

    return build_history(
        [
            a("2024Q1", "2024-05-01 17:00", [1.0, 0.5, 0.9], [100.0, 90.0, 95.0], [10.0, 5.0, 9.0], [500.0, 480.0]),
            a("2024Q2", "2024-08-01 17:00", [2.0, 1.5, 1.0], [110.0, 95.0, 100.0], [20.0, 15.0, 10.0], [520.0, 480.0]),
            a("2024Q3", "2024-11-01 17:00", [3.0, 2.5, 2.0], [120.0, 100.0, 110.0], [30.0, 25.0, 20.0], [540.0, 480.0]),
            a("2024Q4", "2025-02-01 17:00", [4.0, 3.5, 3.0], [130.0, 105.0, 120.0], [40.0, 35.0, 30.0], [560.0, 480.0], provisional=provisional),
        ]
    )


def test_ttm_frame_lists_components_and_formulas_that_add_up():
    h = history()
    row = h.ttm_frame().iloc[-1]
    assert [row[f"period_{k}"] for k in range(1, 5)] == ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]
    assert [row[f"eps_{k}"] for k in range(1, 5)] == [1.0, 2.0, 3.0, 4.0]
    assert row["eps_ttm"] == 10.0 and row["eps_ttm_formula"] == "2024Q1 1 + 2024Q2 2 + 2024Q3 3 + 2024Q4 4 = 10"
    assert row["revenue_ttm"] == 460.0 and row["shares"] == 10.0  # 股数 = 40 ÷ 4
    assert row["shares_formula"] == "公告没有披露股数，用归母净利润 40 ÷ 基本 EPS 4 反推 = 10（2024Q4，百万股；ADR 为百万份 ADS）"
    assert row["shares_source"] == "反推"
    assert row["revenue_per_share_formula"] == "460 ÷ 10 = 46"
    assert row["book_value_per_share_formula"] == "560 ÷ 10 = 56"


def test_explain_lines_for_each_per_share_item():
    rec = history(provisional=True).ttm_detail[-1]
    assert explain("eps_ttm", rec)[0].endswith("= 10")
    ps = explain("revenue_per_share_ttm", rec)
    assert ps[0].startswith("每股营收 TTM = 营收 TTM 460 ÷ 股数 10") and "2024Q1 100" in ps[1]
    pb = explain("book_value_per_share", rec)
    assert pb[0].startswith("每股净资产 = 期末归母净资产 560 ÷ 股数 10")
    assert any("暂用初步数据" in line for line in pb)


def history_with_reported_shares(basis=""):
    def a(period, published, eps, profit, equity, shares, **kw):
        return Announcement(pd.Period(period, freq="Q"), pd.Timestamp(published), f"u-{period}", "CNY", True,
                            {"eps": eps, "revenue": [100.0] * len(eps), "profit": profit, "equity": equity, "wa_shares": shares}, **kw)

    return build_history(
        [
            a("2024Q1", "2024-05-01 17:00", [1.0, 0.5], [10.0, 5.0], [500.0, 480.0], [10.2, 10.4]),
            a("2024Q2", "2024-08-01 17:00", [2.0, 1.5], [20.0, 15.0], [520.0, 480.0], [10.1, 10.3]),
            a("2024Q3", "2024-11-01 17:00", [3.0, 2.5], [30.0, 25.0], [540.0, 480.0], [10.0, 10.2]),
            a("2024Q4", "2025-02-01 17:00", [4.0, 3.5], [40.0, 35.0], [560.0, 480.0], [9.9, 10.1], shares_basis=basis),
        ]
    )


def test_reported_weighted_average_shares_are_used_before_the_implied_ones():
    h = history_with_reported_shares()
    row = h.ttm_frame().iloc[-1]
    assert row["shares"] == 9.9 and row["shares_source"] == "公告披露" and row["shares_implied"] == 10.0  # 反推的 40 ÷ 4 只作对照
    assert row["book_value_per_share"] == round(560.0 / 9.9, 4)
    assert row["shares_formula"].startswith("公告披露的2024Q4基本 EPS 加权平均股数 9.9")
    assert "对照：归母净利润 40 ÷ 基本 EPS 4 反推 10" in row["shares_formula"]


def test_full_year_shares_are_turned_into_fourth_quarter_shares():
    """第四季度公告只披露全年加权平均股数：本期 = (全年 × 全年天数 − 前三季度各自股数 × 天数) ÷ 第四季度天数。"""
    def a(period, published, shares, basis=""):
        return Announcement(pd.Period(period, freq="Q"), pd.Timestamp(published), f"u-{period}", "CNY", True,
                            {"eps": [1.0, 1.0], "revenue": [100.0, 100.0], "profit": [10.0, 10.0], "equity": [500.0, 480.0], "wa_shares": shares},
                            shares_basis=basis)

    h = build_history(
        [
            a("2024Q1", "2024-05-01 17:00", [10.2, 10.4]),
            a("2024Q2", "2024-08-01 17:00", [10.1, 10.3]),
            a("2024Q3", "2024-11-01 17:00", [10.05, 10.2]),
            a("2024Q4", "2025-02-01 17:00", [10.0, 10.3], "全年"),  # 2024 年 366 天：91 + 91 + 92 + 92
        ]
    )
    row = h.ttm_frame().iloc[-1]
    expected = (10.0 * 366 - (10.2 * 91 + 10.1 * 91 + 10.05 * 92)) / 92
    assert row["shares"] == pytest.approx(expected, abs=1e-3) and row["shares_source"] == "由全年数推算"
    assert "（10 × 366 天 − （2024Q1 10.2 × 91 天 + 2024Q2 10.1 × 91 天 + 2024Q3 10.05 × 92 天））÷ 92 天 = 9.653" in row["shares_formula"]

    # 前三季度没有披露股数时，不用全年数硬凑，改为反推
    h = build_history([a("2024Q4", "2025-02-01 17:00", [10.0, 10.3], "全年")])
    assert h.ttm_frame().iloc[-1]["shares_source"] == "反推"


def test_share_counts_are_multiplied_by_splits():
    from valuation_monitor.filings import adjust_for_splits

    a = Announcement(pd.Period("2018Q1", freq="Q"), pd.Timestamp("2018-04-26 10:00"), "u", "KRW", True,
                     {"eps": [85435.0], "wa_shares": [135.92]})
    adjusted = adjust_for_splits([a], pd.Series([50.0], index=[pd.Timestamp("2018-05-04")]))[0]
    assert adjusted.values["eps"] == [1708.7] and adjusted.values["wa_shares"] == pytest.approx([6796.0])
