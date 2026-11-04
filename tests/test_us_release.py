"""Earnings press releases of US companies (8-K exhibit 99.1): Meta Platforms and Cognizant."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.us_release import parse_us_release

ROOT = Path(__file__).resolve().parent.parent
FILINGS = ROOT / "data" / "filings"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def release(income, balance, unit="(In millions, except per share amounts)"):
    return f"<html><body><p>EX-99.1</p><p>{unit}</p>{income}<p>{unit}</p>{balance}</body></html>".encode()


META_INCOME = table(
    ["CONDENSED CONSOLIDATED STATEMENTS OF INCOME"],
    ["(In millions, except per share amounts)"],
    ["Three Months Ended September 30,", "Nine Months Ended September 30,"],
    ["2026", "2025", "2026", "2025"],
    ["Revenue", "64,601", "51,242", "181,712", "141,073"],
    ["Net income", "9,860", "2,709", "52,481", "37,690"],
    ["Earnings per share:"],
    ["Basic", "3.86", "1.08", "20.64", "14.96"],
    ["Diluted", "3.83", "1.05", "20.44", "14.62"],
    ["Weighted-average shares used to compute earnings per share:"],
    ["Basic", "2,552", "2,517", "2,543", "2,520"],
    ["Diluted", "2,573", "2,572", "2,568", "2,578"],
)
META_BALANCE = table(
    ["CONDENSED CONSOLIDATED BALANCE SHEETS"],
    ["September 30, 2026", "December 31, 2025"],
    ["Total assets", "478,836", "366,021"],
    ["Total stockholders' equity", "272,386", "217,243"],
)


def test_meta_layout_in_millions():
    p = parse_us_release(release(META_INCOME, META_BALANCE))
    assert p.period == pd.Period("2026Q3") and p.currency == "USD" and p.quarterly
    assert p.values == {"revenue": [64601.0, 51242.0], "profit": [9860.0, 2709.0], "eps": [3.86, 1.08], "wa_shares": [2552.0, 2517.0], "equity": [272386.0, 217243.0]}


def test_older_cognizant_layout_in_thousands_with_the_date_in_a_second_row_and_a_loss():
    income = table(
        ["Three Months Ended", "Nine Months Ended"],
        ["September 30,", "September 30,"],
        ["2014", "2013", "2014", "2013"],
        ["Revenues", "2,581,009", "2,305,723", "7,520,451", "6,487,701"],
        ["Net income (loss)", "(18,000", "416,000", "1,504,000", "1,553,000"],
        ["Basic earnings (losses) per share", "(0.03", "0.69", "2.54", "2.56"],
        ["Weighted average number of common shares outstanding - Basic", "605,000", "607,000", "593,000", "607,000"],
    )
    balance = table(["September 30,", "December 31,"], ["2014", "2013"], ["Total Liabilities and Stockholders' Equity", "9,227,043", "8,134,718"], ["Stockholders' Equity", "7,381,710", "6,135,791"])
    p = parse_us_release(release(income, balance, "(in thousands, except per share data)"))
    assert p.period == pd.Period("2014Q3")
    assert p.values["revenue"] == pytest.approx([2581.009, 2305.723]) and p.values["profit"] == pytest.approx([-18.0, 416.0])
    assert p.values["eps"] == [-0.03, 0.69] and p.values["wa_shares"] == pytest.approx([605.0, 607.0])  # shares in thousands become millions
    assert p.values["equity"] == pytest.approx([7381.71, 6135.791])


def test_footnote_marks_in_headers_are_ignored():
    income = META_INCOME.replace("<td>2026</td>", "<td>2026 (a)</td>", 1).replace("<td>2025</td>", "<td>2025*</td>", 1)
    balance = META_BALANCE.replace("September 30, 2026", "September 30, 2026 (1)")
    assert parse_us_release(release(income, balance)).period == pd.Period("2026Q3")


def test_a_fourth_quarter_release_takes_the_three_month_group_not_the_year():
    income = META_INCOME.replace("Nine Months Ended September 30,", "Year Ended December 31,").replace("Three Months Ended September 30,", "Three Months Ended December 31,")
    balance = META_BALANCE.replace("September 30, 2026", "December 31, 2026").replace("December 31, 2025", "December 31, 2025")
    p = parse_us_release(release(income, balance))
    assert p.period == pd.Period("2026Q4") and p.values["revenue"] == [64601.0, 51242.0]


def test_missing_data_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="income statement"):
        parse_us_release(release(table(["Revenue", "1"]), META_BALANCE))
    with pytest.raises(ValueError, match="balance sheet"):
        parse_us_release(release(META_INCOME, table(["Total assets", "1", "2"])))
    with pytest.raises(ValueError, match="balance sheet is for"):
        parse_us_release(release(META_INCOME, META_BALANCE.replace("September 30, 2026", "June 30, 2026")))


def saved(symbol):
    index = [e for e in json.loads((FILINGS / symbol / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return {pd.Timestamp(e["published"]): parse_us_release((FILINGS / symbol / e["file"]).read_bytes()) for e in index}


@pytest.mark.parametrize("symbol, restated", [("META", {"2016Q1", "2016Q2", "2016Q3"}), ("CTSH", set())])
def test_every_saved_release_parses_and_agrees_with_the_year_after(symbol, restated):
    """Each quarter appears in its own release and as the prior-year figure of the release four quarters later: they must agree.

    Meta restated its 2016 quarters for a new accounting rule (ASU 2016-09, adopted in the fourth quarter of 2016), so those three differ.
    """
    parsed = saved(symbol)
    periods = sorted(p.period for p in parsed.values())
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))  # no quarter missing
    assert periods[0] <= pd.Period("2014Q4") and periods[-1] >= pd.Period("2026Q3")
    by_period = {p.period: p for p in parsed.values()}
    differing = set()
    for p in parsed.values():
        later = by_period.get(p.period + 4)
        if later is None:
            continue
        for key in ("revenue", "profit", "eps"):
            tolerance = 0.006 if key == "eps" else max(1.0, abs(p.values[key][0]) * 0.001)  # amounts: Cognizant moved from thousands to rounded millions in 2016
            if abs(p.values[key][0] - later.values[key][1]) > tolerance:
                differing.add(str(p.period))
    assert differing == restated


def test_cognizants_2014_figures_are_in_millions_like_the_rest():
    parsed = saved("CTSH")
    first = min(parsed.values(), key=lambda p: p.period)
    assert 1_000 < first.values["revenue"][0] < 10_000 and 100 < first.values["wa_shares"][0] < 1_000  # thousands were converted
