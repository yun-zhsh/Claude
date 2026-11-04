"""Extract earnings data from condensed US GAAP income statements and balance sheets in HTML: the earnings press releases of US companies
(exhibit 99.1 of Form 8-K; Meta Platforms, Cognizant) and the US GAAP summary financial statements that ASML furnishes with each quarterly 6-K (in euros).

The document holds a condensed income statement and a condensed balance sheet as tables:

  income statement   a header with the groups ("Three Months Ended September 30," and "Nine Months Ended September 30," or "Year Ended ...") and one
                     column per year, then rows: revenue, net income, basic earnings per share and the basic weighted-average shares.
                     The "three months" group has this quarter and the same quarter last year.
  balance sheet      two columns, the end of this quarter and the end of last year; the row "Total stockholders' equity".

Amounts are converted to millions (the unit is in the table or the line before it), shares are in millions, per-share data is as published.
ASML's fiscal quarters end on a Sunday near the calendar quarter end (for example July 3, 2022), so a date belongs to the quarter ten days before it; its dates
are one per column ("Jul 4," ... "Jul 3,") and its newest releases (2025 on) have no tables at all, only the text of the statements (parse_text).
The same value in two tables must agree and a missing value is an error: nothing is guessed.
"""

from __future__ import annotations

import re

import pandas as pd

from valuation_monitor.parsed import Parsed
from valuation_monitor.press_release import MONTHS, UNIT_RE, clean, parse_date, table_rows, to_number

GROUP_RE = re.compile(r"^(?P<kind>three|six|nine|twelve) months ended (?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}),?$|^(?P<year_kind>year) ended (?P<ymonth>[A-Z][a-z]+)\.? (?P<yday>\d{1,2}),?$", re.I)
KIND_RE = re.compile(r"^(?P<kind>three|six|nine|twelve) months ended,?$|^(?P<year>year) ended,?$", re.I)  # older releases put the date in the next row
DAY_RE = re.compile(r"^(?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}),?$")
FULL_DATE_RE = re.compile(r"^(?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}), (?P<year>\d{4})$")
MONTH_NUMBER = {**MONTHS, **{name[:3]: number for name, number in MONTHS.items()}}  # "Sep" as well as "September"
YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
REVENUE_RE = re.compile(r"^(?:total )?(?:net )?(?:revenues?|sales)$", re.I)
PROFIT_RE = re.compile(r"^net (?:\(loss\) )?income(?: \(loss\))?(?: attributable to (?!.*non-?controlling).*)?$", re.I)
BASIC_EPS_RE = re.compile(r"^basic (?:\((?:loss|losses)\) )?(?:earnings|net income)(?: \((?:loss|losses)\))? per (?:common |ordinary )?share$", re.I)
BASIC_SHARES_RE = re.compile(r"^weighted[- ]average (?:number of )?(?:common |ordinary )?shares.*\bbasic\b", re.I)
EQUITY_RE = re.compile(r"^(?:total )?(?:stockholders|shareholders).? equity$", re.I)
TOLERANCE = 0.001
NOTE_SUFFIX_RE = re.compile(r"\s+\d(?:,\d)*$")  # footnote numbers after a label: "Diluted net income per ordinary share 4"


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


def currency_of(rows: list[list[str]]) -> str:
    """EUR if the unit line of the table says euros (ASML: "in millions EUR", "in millions €"), else USD."""
    return "EUR" if any(re.search(r"\bEUR\b|€", cell) for row in rows[:8] for cell in row) else "USD"


def header_cell(cell: str) -> str:
    """A header cell without its footnote mark: "2019*" and "December 31, 2016 (1)" become "2019" and "December 31, 2016"."""
    return re.sub(r"\s*(?:\([\da-z]\)|\*+|†)+$", "", cell).strip()


def numbers(row: list[str]) -> list[float]:
    return [v for cell in row[1:] if (v := to_number(cell)) is not None]


def full_date(cell: str) -> pd.Timestamp | None:
    """"December 31, 2016" or "Dec 31, 2014"."""
    m = FULL_DATE_RE.match(header_cell(cell))
    if not m or m.group("month") not in MONTH_NUMBER:
        return None
    return pd.Timestamp(year=int(m.group("year")), month=MONTH_NUMBER[m.group("month")], day=int(m.group("day")))


def days_in(cells: list[str]) -> list[tuple[int, int]]:
    """The (month, day) of every cell like "September 30," or "Jul 4,"."""
    out = []
    for cell in cells:
        m = DAY_RE.match(header_cell(cell))
        if m and m.group("month") in MONTH_NUMBER:
            out.append((MONTH_NUMBER[m.group("month")], int(m.group("day"))))
    return out


def years_in(cells: list[str]) -> list[int]:
    """The years of a header row; "2017 3" is the year 2017 with the footnote number 3."""
    return [int(m.group(1)) for c in cells if (m := re.match(r"^((?:19|20)\d\d)(?:\s+\d)?$", header_cell(c)))]


def quarter_of(day: pd.Timestamp) -> pd.Period:
    """ASML's fiscal quarters end on the Sunday nearest to the calendar quarter end (July 3, 2022; January 2, 2022 for the fourth quarter of 2021)."""
    return pd.Period(day - pd.Timedelta(days=10), freq="Q")


def income_columns(rows: list[list[str]]) -> tuple[int, list[pd.Timestamp]] | None:
    """(index of the first data row, the period-end date of each value column of the three-months group) of an income statement table.

    Forms of the header: groups with their date ("Three Months Ended September 30,"), groups and in the next row their dates, groups and in the next
    row one date per column ("Jul 4," "Jul 3," ...), or one full date per column ("Dec 31, 2014"); the years are in a row of their own or in the full dates.
    """
    for i, row in enumerate(rows[:10]):
        cells = [header_cell(c) for c in row]
        matches = [m for c in cells if (m := GROUP_RE.match(c))]
        kinds = [(m.group("kind") or "year").lower() for m in matches]
        group_days = [(MONTH_NUMBER[(m.group("month") or m.group("ymonth")).capitalize()], int(m.group("day") or m.group("yday"))) for m in matches]
        if not kinds:
            kinds = [(m.group("kind") or "year").lower() for c in cells if (m := KIND_RE.match(c))]
            group_days = []
        if not kinds or "three" not in kinds:
            continue
        three = kinds.index("three")
        for j in range(i, min(i + 4, len(rows))):
            cells_j = [header_cell(c) for c in rows[j]]
            years = years_in(cells_j)
            dates = [d for c in cells_j if (d := full_date(c))]
            per_group = None
            if len(dates) >= 2 and len(dates) % len(kinds) == 0:  # one full date per column
                n = len(dates) // len(kinds)
                return j + 1, dates[three * n : (three + 1) * n]
            if len(years) >= 2 and len(years) % len(kinds) == 0:
                n = len(years) // len(kinds)
                days = group_days or days_in([header_cell(c) for r in rows[i + 1 : j + 1] for c in r])  # the day of each group, or of each column
                if len(days) == len(kinds):
                    per_group = [days[three]] * n
                elif len(days) == len(years):
                    per_group = days[three * n : (three + 1) * n]
                if per_group is None:
                    continue
                return j + 1, [pd.Timestamp(year=y, month=month, day=day) for y, (month, day) in zip(years[three * n : (three + 1) * n], per_group)]
    return None


def parse_income(rows: list[list[str]], scale: float | None) -> tuple[pd.Period, dict[str, list[float]]] | None:
    header = income_columns(rows)
    if header is None or scale is None:
        return None
    first, columns = header
    found: dict[str, list[float]] = {}
    section = ""
    for row in rows[first:]:
        label = NOTE_SUFFIX_RE.sub("", row[0])
        low = label.lower()
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
    periods = [quarter_of(d) for d in columns]
    period = max(periods)
    order = [periods.index(q) for q in (period, period - 4, period - 1) if q in periods]  # this quarter, the same quarter last year, and the previous quarter if the table has it
    if len(order) < 2 or periods.index(period - 4) != order[1]:
        return None
    values = {}
    for key, nums in found.items():
        picked = [nums[c] for c in order]
        values[key] = [v * scale for v in picked] if key in ("revenue", "profit", "wa_shares") else picked
    return period, values


def balance_dates(rows: list[list[str]]) -> list[pd.Timestamp] | None:
    """The date of each value column of a balance sheet: full dates in a header row, or the days in one row and the years in the next."""
    for r in rows[:8]:
        dates = [d for c in r if (d := full_date(c))]
        if len(dates) >= 2:
            return dates
    for r, below in zip(rows[:7], rows[1:8]):
        days, years = days_in([header_cell(c) for c in r]), years_in(below)
        if len(days) >= 2 and len(days) == len(years):
            return [pd.Timestamp(year=y, month=m, day=d) for (m, d), y in zip(days, years)]
    return None


def parse_balance(rows: list[list[str]], scale: float | None) -> tuple[pd.Period, list[float]] | None:
    """Equity at the end of this period and at the end of last year."""
    if scale is None:
        return None
    dates = balance_dates(rows)
    row = next((r for r in rows if EQUITY_RE.match(NOTE_SUFFIX_RE.sub("", r[0])) and len(numbers(r)) >= 2), None)  # older releases: "Stockholders' Equity" (the heading has no numbers)
    if not dates or row is None:
        return None
    nums = numbers(row)[: len(dates)]
    if len(nums) != len(dates):
        return None
    order = sorted(range(len(dates)), key=lambda c: dates[c], reverse=True)  # the end of this period first (ASML lists the year end first)
    return quarter_of(dates[order[0]]), [nums[order[0]] * scale, nums[order[1]] * scale]


def close(a: float, b: float, key: str) -> bool:
    return abs(a - b) <= (0.005 if key == "eps" else max(1.0, abs(a) * TOLERANCE))


NUMBER = r"\(?-?[\d,]+(?:\.\d+)?\)?"


def text_numbers(match: re.Match, count: int) -> list[float]:
    return [to_number(match.group(k)) for k in range(1, count + 1)]


def parse_text(html: bytes) -> Parsed | None:
    """ASML's newest statements (2025 on) are positioned text without tables: the rows are found in the text of the page."""
    import html as htmllib

    text = clean(htmllib.unescape(re.sub(r"<[^>]+>", " ", html.decode("utf-8", "replace"))))
    start = text.find("Net system sales")  # the first statements: the later quarterly summary has five quarters
    if start < 0:
        return None
    block = text[max(0, start - 600) : start + 3500]
    header = re.search(r"((?:[A-Z][a-z]{2} \d{1,2}, ){2,4})\([Uu]naudited,[^)]*in millions[^)]*\) ((?:(?:19|20)\d\d ){2,4})", block)
    unit = re.search(r"in (millions|thousands)", block)
    if header is None or unit is None:
        return None
    days = [(MONTH_NUMBER[m.group(1)], int(m.group(2))) for m in re.finditer(r"([A-Z][a-z]{2}) (\d{1,2}),", header.group(1))]
    years = [int(y) for y in header.group(2).split()]
    if len(days) != len(years):
        return None
    groups = max(1, len(re.findall(r"(?:Three|Six|Nine|Twelve) months ended", block[max(0, header.start() - 120) : header.start()], re.I)))  # the first quarter has the three-months group only
    if len(days) % groups:
        return None
    n = len(days) // groups
    columns = [pd.Timestamp(year=y, month=m, day=d) for (m, d), y in zip(days[:n], years[:n])]  # the three-months group first
    scale = 0.001 if unit.group(1) == "thousands" else 1.0

    def row(label: str, count: int) -> list[float] | None:
        m = re.search(rf"{label} ({NUMBER}) ({NUMBER})(?: ({NUMBER}))?(?: ({NUMBER}))?", block[block.find("Net system sales") :])
        return None if m is None else [to_number(g) for g in m.groups()[:count] if g is not None]

    revenue, profit, eps = row("Total net sales", n * 2), row("Net income", n * 2), row("Basic net income per ordinary share", n * 2)
    shares_at = block.find("Basic net income per ordinary share")
    shares = re.search(rf"(?:in millions\) used in computing per share amounts|computing per share amounts \(in millions\)):? Basic ({NUMBER}) ({NUMBER})", block[shares_at:])
    if None in (revenue, profit, eps) or shares is None or len(revenue) < n:
        return None
    equity = re.search(rf"Total shareholders. equity ({NUMBER}) ({NUMBER})", text[text.find("Total shareholders"):] if "Total shareholders" in text else "")
    if equity is None:
        return None
    ordered = sorted(range(n), key=lambda c: columns[c], reverse=True)
    pick = lambda vals: [vals[c] for c in ordered[:2]]  # noqa: E731
    values = {
        "revenue": [v * scale for v in pick(revenue)],
        "profit": [v * scale for v in pick(profit)],
        "eps": pick(eps),
        "wa_shares": [v * scale for v in pick([to_number(shares.group(1)), to_number(shares.group(2))])],
        "equity": [to_number(equity.group(2)) * scale, to_number(equity.group(1)) * scale],  # the year end is listed first, then this quarter's end
    }
    return Parsed("EUR", values, quarter_of(columns[ordered[0]]))


def parse_us_release(html: bytes) -> Parsed:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    income: list[tuple[pd.Period, dict[str, list[float]], str]] = []
    balance: list[tuple[pd.Period, list[float]]] = []
    for table in soup.find_all("table"):
        rows = table_rows(table)
        if not rows:
            continue
        scale = unit_scale(table, rows)
        if (found := parse_income(rows, scale)) is not None:
            income.append((*found, currency_of(rows)))
        if (found := parse_balance(rows, scale)) is not None:
            balance.append(found)
    if not income:
        if not soup.find("table") or "Net system sales" in soup.get_text(" "):
            if (parsed := parse_text(html)) is not None:
                return parsed
        raise ValueError("Not found: the income statement (revenue, net income, basic EPS and basic shares for three months)")
    if not balance:
        raise ValueError("Not found: the balance sheet (total stockholders' equity)")
    (period, values, currency), *more = income
    for p, other, _ in more:
        if p == period and any(not close(a, b, key) for key, vals in other.items() for a, b in zip(vals, values[key])):
            raise ValueError(f"The income statements in this release disagree: {values} and {other}")
        if p == period and len(next(iter(other.values()))) > len(next(iter(values.values()))):
            values = other  # the table with the previous quarter as well (ASML's quarterly summary) has more to offer; the overlap was checked above
    equity_period, equity = balance[0]
    if equity_period != period:
        raise ValueError(f"The balance sheet is for {equity_period}, the income statement for {period}")
    return Parsed(currency, {**values, "equity": equity}, period)
