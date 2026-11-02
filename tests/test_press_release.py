"""Verify the parsing with the JD earnings press releases saved in the repo (data/filings/JD); the numbers were checked against the press releases and the 20-F annual reports."""

import json
from functools import cache
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.press_release import parse_press_release

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "JD"
pytestmark = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved JD filings")


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
    assert p.values["eps"] == [5.30, 4.35]  # basic EPS per ADS (diluted 5.01)
    assert p.values["non_ifrs_eps"] == [6.63, 5.20]
    assert p.values["equity"] == [219118, 225040]  # 2026-06-30, 2025-12-31


def test_2014q2_in_thousands_and_old_labels():
    p = parsed("2014-08-18")
    assert p.period == pd.Period("2014Q2")
    assert p.values["revenue"] == pytest.approx([28612.850, 17453.166])  # thousands converted to millions
    assert p.values["profit"] == pytest.approx([-7046.296, -484.213])  # attributable to permanent equity holders after deducting the preferred share accretion
    assert p.values["eps"] == [-5.86, -0.57]
    assert p.values["equity"] == pytest.approx([37835.659, 2066.565])  # there is no non-controlling interest row; take total shareholders' equity


def test_discontinued_operations_use_totals():
    p = parsed("2017-08-16")  # the income statement is split into continuing and discontinued operations; take the total
    assert p.values["profit"] == pytest.approx([-496.376, -252.309])
    assert p.values["eps"] == [-0.35, -0.18]


def test_three_quarter_columns_are_ordered_by_date():
    p = parsed("2017-03-06")  # the header is same quarter last year, previous quarter, this quarter
    assert p.period == pd.Period("2016Q4")
    assert p.values["eps"] == [-1.26, -5.57, -0.64]


def test_quarters_add_up_to_20f_annual_figures():
    """The quarterly press releases add up to the full-year figures of the 20-F annual report (XBRL) (no restatement from 2018 on)."""
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
