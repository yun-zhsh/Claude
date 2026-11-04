"""Extract earnings data from the earnings press releases (HTML, exhibit 99.1 of Form 8-K) of US companies that report in US dollars under US GAAP
(Meta Platforms, Cognizant).

The press release holds a condensed income statement and a condensed balance sheet as tables:

  income statement   a header with the groups ("Three Months Ended September 30," and "Nine Months Ended September 30," or "Year Ended ...") and one
                     column per year, then rows: revenue, net income, basic earnings per share and the basic weighted-average shares.
                     The "three months" group has this quarter and the same quarter last year.
  balance sheet      two columns, the end of this quarter and the end of last year; the row "Total stockholders' equity".

Amounts are converted to millions (the unit is in the table or the line before it), shares are in millions, per-share data is as published.
The same value in two tables must agree and a missing value is an error: nothing is guessed.
"""

from __future__ import annotations

import re

import pandas as pd

from valuation_monitor.parsed import Parsed
from valuation_monitor.press_release import MONTHS, UNIT_RE, clean, parse_date, table_rows, to_number

GROUP_RE = re.compile(r"^(?P<kind>three|six|nine|twelve) months ended (?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}),?$|^(?P<year_kind>year) ended (?P<ymonth>[A-Z][a-z]+)\.? (?P<yday>\d{1,2}),?$", re.I)
KIND_RE = re.compile(r"^(?P<kind>three|six|nine|twelve) months ended$|^(?P<year>year) ended$", re.I)  # older releases put the date in the next row
DAY_RE = re.compile(r"^(?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}),?$")
YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
REVENUE_RE = re.compile(r"^(?:total )?(?:net )?revenues?$", re.I)
PROFIT_RE = re.compile(r"^net (?:\(loss\) )?income(?: \(loss\))?(?: attributable to (?!.*non-?controlling).*)?$", re.I)
BASIC_EPS_RE = re.compile(r"^basic (?:\((?:loss|losses)\) )?(?:earnings|net income)(?: \((?:loss|losses)\))? per (?:common )?share$", re.I)
BASIC_SHARES_RE = re.compile(r"^weighted[- ]average (?:number of )?(?:common )?shares.*\bbasic\b", re.I)
EQUITY_RE = re.compile(r"^(?:total )?stockholders.? equity$", re.I)
TOLERANCE = 0.001


def unit_scale(table, rows: list[list[str]]) -> float | None:
    """Factor converting the amounts of a table to millions; None if the unit is not stated."""
    for row in rows[:8]:
        for cell in row:
            if m := UNIT_RE.search(cell):
                return 0.001 if m.group(1).lower() == "thousands" else 1.0
    for s in table.find_all_previous(string=True, limit=40):
        if m := UNIT_RE.search(clean(s)):
            return 0.001 if m.group(1).lower() == "thousands" else 1.0
    return None


def header_cell(cell: str) -> str:
    """A header cell without its footnote mark: "2019*" and "December 31, 2016 (1)" become "2019" and "December 31, 2016"."""
    return re.sub(r"\s*(?:\([\da-z]\)|\*+|†)+$", "", cell).strip()


def numbers(row: list[str]) -> list[float]:
    return [v for cell in row[1:] if (v := to_number(cell)) is not None]


def income_columns(rows: list[list[str]]) -> tuple[int, list[tuple[int, int]]] | None:
    """(index of the first data row, (month, year) of each value column of the three-months group, this quarter first) of an income statement table."""
    for i, row in enumerate(rows[:10]):
        cells = [header_cell(c) for c in row]
        groups = [(m.group("kind") or "year").lower() for c in cells if (m := GROUP_RE.match(c))]  # "Three Months Ended September 30,"
        months = [MONTHS[(m.group("month") or m.group("ymonth")).capitalize()] for c in cells if (m := GROUP_RE.match(c))]
        if not groups:  # "Three Months Ended" and, in the next row, "September 30,"
            groups = [(m.group("kind") or "year").lower() for c in cells if (m := KIND_RE.match(c))]
            if groups and i + 1 < len(rows):
                months = [MONTHS[m.group("month")] for c in rows[i + 1] if (m := DAY_RE.match(header_cell(c))) and m.group("month") in MONTHS]
        if not groups or len(months) != len(groups):
            continue
        for j in range(i, min(i + 4, len(rows))):
            years = [int(header_cell(c)) for c in rows[j] if YEAR_RE.match(header_cell(c))]
            if len(years) >= 2 and len(years) % len(groups) == 0:
                per_group = len(years) // len(groups)
                if "three" not in groups:
                    return None
                three = groups.index("three")
                return j + 1, [(months[three], y) for y in years[three * per_group : (three + 1) * per_group]]
    return None


def parse_income(rows: list[list[str]], scale: float | None) -> tuple[pd.Period, dict[str, list[float]]] | None:
    header = income_columns(rows)
    if header is None or scale is None:
        return None
    first, columns = header
    found: dict[str, list[float]] = {}
    section = ""
    for row in rows[first:]:
        label, low = row[0], row[0].lower()
        nums = numbers(row)
        if not nums:
            if "per share" in low and "weighted" not in low:
                section = "eps"
            elif re.match(r"weighted[- ]average", low):
                section = "shares"
            continue
        if len(nums) < len(columns):
            continue
        mine = nums[: len(columns)]
        if REVENUE_RE.match(label):
            found.setdefault("revenue", mine)
        elif PROFIT_RE.match(label):
            found.setdefault("profit", mine)
        elif BASIC_EPS_RE.match(label) or (section == "eps" and low == "basic"):
            found.setdefault("eps", mine)
        elif BASIC_SHARES_RE.match(label) or (section == "shares" and low == "basic"):
            found.setdefault("wa_shares", mine)
    if not {"revenue", "profit", "eps", "wa_shares"} <= found.keys():
        return None
    ordered = sorted(range(len(columns)), key=lambda c: columns[c][1], reverse=True)  # this quarter first
    this_month, this_year = columns[ordered[0]]
    period = pd.Period(year=this_year, quarter=(this_month - 1) // 3 + 1, freq="Q")
    values = {}
    for key, nums in found.items():
        picked = [nums[c] for c in ordered[:2]]
        values[key] = [v * scale for v in picked] if key in ("revenue", "profit", "wa_shares") else picked
    return period, values


def parse_balance(rows: list[list[str]], scale: float | None) -> tuple[pd.Period, list[float]] | None:
    """Equity at the end of this period and at the end of last year."""
    if scale is None:
        return None
    dates = next(([d for c in r if (d := parse_date(header_cell(c)))] for r in rows[:8] if sum(1 for c in r if parse_date(header_cell(c))) >= 2), None)
    if dates is None:  # older releases: "September 30," and "December 31," in one row, the years in the next
        for r, below in zip(rows[:7], rows[1:8]):
            days = [m for c in r if (m := DAY_RE.match(header_cell(c))) and m.group("month") in MONTHS]
            years = [int(header_cell(c)) for c in below if YEAR_RE.match(header_cell(c))]
            if len(days) >= 2 and len(days) == len(years):
                dates = [pd.Timestamp(year=y, month=MONTHS[m.group("month")], day=int(m.group("day"))) for m, y in zip(days, years)]
                break
    row = next((r for r in rows if EQUITY_RE.match(r[0]) and len(numbers(r)) >= 2), None)  # older releases: "Stockholders' Equity" (the heading has no numbers)
    if not dates or row is None:
        return None
    nums = numbers(row)
    return pd.Period(dates[0], freq="Q"), [nums[0] * scale, nums[1] * scale]


def close(a: float, b: float, key: str) -> bool:
    return abs(a - b) <= (0.005 if key == "eps" else max(1.0, abs(a) * TOLERANCE))


def parse_us_release(html: bytes) -> Parsed:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    income: list[tuple[pd.Period, dict[str, list[float]]]] = []
    balance: list[tuple[pd.Period, list[float]]] = []
    for table in soup.find_all("table"):
        rows = table_rows(table)
        if not rows:
            continue
        scale = unit_scale(table, rows)
        if (found := parse_income(rows, scale)) is not None:
            income.append(found)
        if (found := parse_balance(rows, scale)) is not None:
            balance.append(found)
    if not income:
        raise ValueError("Not found: the income statement (revenue, net income, basic EPS and basic shares for three months)")
    if not balance:
        raise ValueError("Not found: the balance sheet (total stockholders' equity)")
    (period, values), *more = income
    for p, other in more:
        if p == period and any(not close(a, b, key) for key, vals in other.items() for a, b in zip(vals, values[key])):
            raise ValueError(f"The income statements in this release disagree: {values} and {other}")
    equity_period, equity = balance[0]
    if equity_period != period:
        raise ValueError(f"The balance sheet is for {equity_period}, the income statement for {period}")
    return Parsed("USD", {**values, "equity": equity}, period)
