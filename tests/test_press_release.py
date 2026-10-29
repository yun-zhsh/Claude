"""用仓库里保存的京东业绩新闻稿原文（data/filings/JD）验证解析，数字都核对过新闻稿原文和 20-F 年报。"""

import json
from functools import cache
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.press_release import parse_press_release

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "JD"
pytestmark = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="没有保存的京东公告")


@cache
def parse_file(name: str):
    return parse_press_release((FOLDER / name).read_bytes())


def results_index():
    return [e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e["results"]]


def parsed(prefix: str):
    return parse_file(next(e for e in results_index() if e["published"].startswith(prefix))["file"])


def test_2026q2_in_millions():
    p = parsed("2026-08-13")
    assert p.period == pd.Period("2026Q2") and p.currency == "CNY"
    assert p.values["revenue"] == [346401, 356660]
    assert p.values["profit"] == [7129, 6178]
    assert p.values["eps"] == [5.30, 4.35]  # 基本 EPS，每份 ADS（摊薄 5.01）
    assert p.values["non_ifrs_eps"] == [6.63, 5.20]
    assert p.values["equity"] == [219118, 225040]  # 2026-06-30、2025-12-31


def test_2014q2_in_thousands_and_old_labels():
    p = parsed("2014-08-18")
    assert p.period == pd.Period("2014Q2")
    assert p.values["revenue"] == pytest.approx([28612.850, 17453.166])  # 千元换算成百万元
    assert p.values["profit"] == pytest.approx([-7046.296, -484.213])  # 扣除优先股增值后归属永久权益持有人
    assert p.values["eps"] == [-5.86, -0.57]
    assert p.values["equity"] == pytest.approx([37835.659, 2066.565])  # 没有非控股权益行，取股东权益合计


def test_discontinued_operations_use_totals():
    p = parsed("2017-08-16")  # 损益表分持续经营和终止经营，取合计
    assert p.values["profit"] == pytest.approx([-496.376, -252.309])
    assert p.values["eps"] == [-0.35, -0.18]


def test_three_quarter_columns_are_ordered_by_date():
    p = parsed("2017-03-06")  # 表头是 去年同季度、上一季度、本季度
    assert p.period == pd.Period("2016Q4")
    assert p.values["eps"] == [-1.26, -5.57, -0.64]


def test_quarters_add_up_to_20f_annual_figures():
    """各季度新闻稿的数字加总，与 20-F 年报（XBRL）的全年数一致（2018 年及以后无重述）。"""
    first = {}
    for e in results_index():
        p = parse_file(e["file"])
        first.setdefault(p.period, p.values)
    annual = {2023: (1084662, 24167), 2024: (1158819, 41359), 2025: (1309085, 19631)}
    for year, (revenue, profit) in annual.items():
        quarters = [first[pd.Period(f"{year}Q{q}")] for q in range(1, 5)]
        assert sum(v["revenue"][0] for v in quarters) == pytest.approx(revenue, abs=2)
        assert sum(v["profit"][0] for v in quarters) == pytest.approx(profit, abs=2)


def test_every_saved_release_parses():
    index = results_index()
    assert len(index) >= 40
    for e in index:
        p = parse_file(e["file"])
        assert {"revenue", "profit", "eps", "equity"} <= p.values.keys(), e["file"]
