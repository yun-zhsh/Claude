import pandas as pd

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
    assert row["shares_formula"].startswith("归母净利润 40 ÷ 基本 EPS 4 = 10")
    assert row["revenue_per_share_formula"] == "460 ÷ 10 = 46"
    assert row["book_value_per_share_formula"] == "560 ÷ 10 = 56"


def test_explain_lines_for_each_per_share_item():
    rec = history(provisional=True).ttm_detail[-1]
    assert explain("eps_ttm", rec)[0].endswith("= 10")
    ps = explain("revenue_per_share_ttm", rec)
    assert ps[0].startswith("每股营收 TTM = 营收 TTM 460 ÷ 股数 10") and "2024Q1 100" in ps[1]
    pb = explain("book_value_per_share", rec)
    assert pb[0].startswith("每股净资产 = 期末归母净资产 560 ÷ 股数 10")
    assert any("暂用业绩发布会材料" in line for line in pb)
