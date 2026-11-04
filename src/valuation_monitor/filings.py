"""Extract earnings data from the original earnings filings and arrange it into per-period (quarter or half year) and TTM data.

Extracted data: revenue, net profit attributable to owners (millions), basic EPS (per share), Non-IFRS/Non-GAAP basic EPS (some companies have none),
equity attributable to owners (millions), shares disclosed in the filing (millions). The filing layout differs by company, so different parse functions handle them:

  highlights (Tencent, PDF, verified on all filings since 2014)
    - Financial highlights table on page 1: revenue, net profit attributable to owners, basic EPS, Non-IFRS basic EPS.
        Quarterly format: three numbers per row: this quarter, same quarter last year, previous quarter;
        cumulative format (interim, Q3 and annual filings before mid-2016): two numbers: this year to date,
        same period last year to date.
    - Balance sheet: equity attributable to owners, for the end of this period and the end of last year. EPS note: weighted average shares.
  statements (Sunny Optical, PDF, verified on all interim and annual filings since 2014)
    - Consolidated income statement: revenue, net profit attributable to owners, basic EPS (unit "RMB cents"), each with two numbers:
      this period to date, same period last year to date; amounts are in thousands and converted to millions when read.
    - Balance sheet: same as above. EPS note: weighted average shares.
  press_release (JD, HTML) is in press_release.py; kifrs_statements (Samsung, PDF) in kifrs.py;
  cn_report (Hikvision, PDF) in cn_report.py. These three parse functions return the common Parsed (see parsed.py).

A company may publish results every quarter (4 periods a year) or only interim and annual (2 periods a year; the second half is obtained as full year minus first half).
The number of periods is inferred from the existing filings, and the TTM is the sum of the periods in the last year (4 quarters or 2 half years).

The same value appears in several filings (this period, the next period's sequential comparison, next year's year-over-year comparison, the balance sheet's end of last year),
and the program compares them one by one; on disagreement the first published value is used and a notice is given.

Shares: the number disclosed in the filing is used (the basic EPS weighted average shares; period-end total shares for A-shares); where Q4 only discloses the full year it is derived by day-weighting;
only if none is disclosed is it implied as "net profit attributable to owners / basic EPS" of the period. The data available after each filing and how it is calculated are in _ttm_row and audit.py.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.market_calendar import HK, Market, effective_trading_day, get_market
from valuation_monitor.cn_report import parse_cn_express, parse_cn_report
from valuation_monitor.audit import Component, num, shares_formula, split_notes, sum_formula
from valuation_monitor.kifrs import parse_kifrs, parse_kifrs_deck
from valuation_monitor.press_release import parse_press_release
from valuation_monitor.us_release import parse_us_release
from valuation_monitor.providers.base import Filing

MONTH_TO_QUARTER = {"MARCH": 1, "JUNE": 2, "SEPTEMBER": 3, "DECEMBER": 4}
PERIOD_RE = re.compile(r"ENDED (\d{1,2}) (MARCH|JUNE|SEPTEMBER|DECEMBER) (\d{4})", re.I)
EPS_RE = re.compile(
    r"(?P<non_ifrs>Non-IFRS |Non-GAAP )?EPS[\s()\d]*\((?P<ccy>[A-Z]{3}) per share\)[\s*]*\S?[–-] ?basic (?P<body>.*?)\S?[–-] ?diluted"
)
NUMBER_RE = re.compile(r"\(?\d+\.\d+\)?")
# Labels of the amount rows in the highlights table: only count when followed by a number (possibly with a footnote marker), to avoid matching the same words in the body text
REVENUE_RE = re.compile(r"\bRevenues? (?=[(*\d])")
PROFIT_RE = re.compile(r"(?<!IFRS )(?<!GAAP )Profit attributable to (?:the Company.s )?equity holders of the Company (?=[(*\d])")
EQUITY_RE = re.compile(
    r"EQUITY Equity attributable to (?:the Company.s )?equity holders(?: of the Company)?(?P<body>.*?)Non-controlling interests"
)
AMOUNT_RE = re.compile(r"\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?")
FOOTNOTE_RE = re.compile(r"\(\d\)|\*+")
CURRENCY_CODES = {"RMB": "CNY", "HK$": "HKD", "HKD": "HKD", "US$": "USD", "USD": "USD"}  # spelling in the filing -> ISO currency code

# Tencent: weighted average shares row in the EPS note (millions), two numbers each for three months and cumulative: this period, same period last year
WA_SHARES_RE = re.compile(
    r"Weighted average number of ordinary shares in issue(?: excluding [^()]*?)?(?:\s*\([^)]*\))*\s*((?:[\d,]+(?:\.\d+)?\s+){1,4})(?=[A-Z])", re.I
)
# Sunny: weighted average shares row in the EPS note (thousands), this period to date, same period last year to date
WA_SHARES_STMT_RE = re.compile(
    r"Weighted average number of ordinary shares for the purpose of basic earnings per share(?: \(Note\))? (?P<a>[\d,]+) (?P<b>[\d,]+)", re.I
)

# statements layout: rows in the consolidated income statement and balance sheet
AMT = r"\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?(?![\d,])"  # amounts with thousands separators; note numbers such as "3," do not count
NOTE = r"\d{1,2}[A-Za-z]?(?:, ?\d{1,2}[A-Za-z]?)*"  # note numbers: 3, 3A, 3, 4, 3,4
OWNERS = r"(?:owners|equity holders|shareholders) of the Company"
REVENUE_STMT_RE = re.compile(rf"\bRevenue (?:{NOTE} )?(?P<a>{AMT}) (?P<b>{AMT})")
PROFIT_STMT_RE = re.compile(rf"attributable to:? {OWNERS} (?P<a>{AMT}) (?P<b>{AMT})", re.I)
EQUITY_STMT_RE = re.compile(rf"Equity attributable to {OWNERS} (?P<a>{AMT}) (?P<b>{AMT})", re.I)
EPS_STMT_RE = re.compile(
    rf"Earnings per share\s*[–-]\s*Basic \((?P<ccy>RMB|HK\$|HKD|US\$|USD)(?P<cents> cents)?\) (?:{NOTE} )?(?P<a>\(?-?[\d.]+\)?) (?P<b>\(?-?[\d.]+\)?)",
    re.I,
)
# Labels before the net profit row. The total comprehensive income row is written exactly like the net profit row, so they can only be told apart by the label
PROFIT_LABEL_RE = re.compile(r"\bprofit(?: \(loss\))? for the (?:period|year)\s*$", re.I)
UNIT_RE = re.compile(r"RMB\W{0,2}(?P<unit>000|million)", re.I)  # amount unit in the header: RMB’000 or RMB million

PER_SHARE = ("eps", "non_ifrs_eps")  # per-share data, which must be adjusted for splits
AMOUNTS = ("revenue", "profit")  # amounts (millions)
FLOWS = PER_SHARE + AMOUNTS  # period data: can be split into quarters and summed into the TTM
BALANCES = ("equity",)  # point-in-time data (millions)
SHARE_COUNTS = ("wa_shares",)  # weighted average shares used for basic EPS as disclosed in the filing (millions; millions of ADS for ADRs), multiplied by the split ratio on a split
SERIES = FLOWS + BALANCES + SHARE_COUNTS
SERIES_LABELS = {
    "eps": "EPS",
    "non_ifrs_eps": "Non-IFRS EPS",
    "revenue": "Revenue",
    "profit": "Net profit attributable to owners",
    "equity": "Equity attributable to owners",
    "wa_shares": "Weighted average shares",
}
PARSER_VERSIONS = {"highlights": 5, "statements": 3, "press_release": 3, "us_press_release": 1, "us_gaap_statements": 1, "kifrs_statements": 4, "cn_report": 3}  # add 1 when the parsing rules change; saved parse results are then re-parsed automatically
PER_SHARE_TOLERANCE = 0.005  # allowed difference of the same value between filings: per-share data (rounding, subtraction of cumulative figures)
PER_SHARE_RELATIVE_TOLERANCE = 5e-4
AMOUNT_TOLERANCE = 0.001  # amounts: relative difference
SHARES_CHANGE_LIMIT = 0.05  # notice when the implied shares change by more than 5% between adjacent quarters (possible extraction error)

# Column order of the downloaded TTM file: filing information first, then the raw data of each period -> TTM total -> formula -> per-share data
TTM_FRAME_ORDER = [
    "report_date", "published", "available_date", "effective_date", "provisional",
    *[f"period_{k}" for k in range(1, 5)],
    *[f"eps_{k}" for k in range(1, 5)], "eps_ttm", "eps_ttm_formula",
    *[f"non_ifrs_eps_{k}" for k in range(1, 5)], "non_ifrs_eps_ttm", "non_ifrs_eps_ttm_formula",
    *[f"revenue_{k}" for k in range(1, 5)], "revenue_ttm", "revenue_ttm_formula",
    "profit_latest", "eps_latest", "shares", "shares_source", "shares_implied", "shares_formula",
    "revenue_per_share_ttm", "revenue_per_share_formula",
    "equity", "book_value_per_share", "book_value_per_share_formula", "split_note", "published_note",
]
TTM_COLUMNS = [
    "report_date", "published", "available_date",
    "eps_ttm", "non_ifrs_eps_ttm", "revenue_ttm", "shares",
    "revenue_per_share_ttm", "equity", "book_value_per_share",
]


PROVISIONAL_NOTES = {
    "KR": "comes from the earnings call presentation (before the external review; revenue and net profit are only precise to KRW 0.1 trillion and equity is an estimate); it is replaced automatically when the English financial statements are out",
    "CN": "comes from the express report (not audited by the accounting firm, so it may differ slightly from the final annual report numbers); it is replaced automatically when the annual report is out",
    "*": "is preliminary and is replaced automatically when the official report is out",
}


def tolerance(series: str, value: float) -> float:
    if series in PER_SHARE:  # per-share data is rounded to the cent or the unit, e.g. Samsung's EPS is a whole KRW figure and a split adjustment leaves a fraction
        return max(PER_SHARE_TOLERANCE, abs(value) * PER_SHARE_RELATIVE_TOLERANCE)
    return max(1.0, abs(value) * AMOUNT_TOLERANCE)


@dataclass
class Announcement:
    period: pd.Period  # reporting quarter of this filing, e.g. 2026Q2
    published: pd.Timestamp  # publication time (Hong Kong time)
    url: str
    currency: str
    is_quarterly_table: bool  # True: the highlights table has single-quarter data; False: year-to-date cumulative data
    values: dict[str, list[float]]  # series -> values of the row (equity: end of this period, end of last year)
    market: str = "HK"  # listing market, decides the closing time
    provisional: bool = False  # preliminary data, replaced when the official report is out
    shares_basis: str = ""  # period the disclosed weighted average shares refer to: empty means this period, "full year" means the full-year figure is disclosed (Q4)
    shares_label: str = "basic EPS weighted average shares"  # what kind of share count the filing discloses (A-share companies disclose the period-end total shares)
    note: str = ""  # explanation of the publication time etc.

    @property
    def available(self) -> pd.Timestamp:
        """The date of the first closing price that can use this filing."""
        return get_market(self.market).available_date(self.published)


@dataclass
class Observation:
    value: float
    url: str
    derived: bool  # whether obtained by subtracting cumulative figures


@dataclass
class QuarterValue:
    value: float
    available: pd.Timestamp
    published: pd.Timestamp  # publication time of the earliest filing that published this value
    observations: list[Observation] = field(default_factory=list)

    @property
    def max_deviation(self) -> float:
        return max(abs(o.value - self.value) for o in self.observations)


def parse_period(title: str) -> pd.Period:
    m = PERIOD_RE.search(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    return pd.Period(year=int(m.group(3)), quarter=MONTH_TO_QUARTER[m.group(2).upper()], freq="Q")


def period_label(p: pd.Period, periods_per_year: int) -> str:
    """Display name of a period: 2025Q4 for quarterly reporters, 2025H2 for half-yearly reporters."""
    if periods_per_year == 4:
        return str(p)
    if periods_per_year == 2:
        return f"{p.year}H{p.quarter // 2}"
    return f"{p.year}FY"


def to_number(token: str) -> float:
    value = float(token.strip("()").replace(",", ""))
    return -value if token.startswith("(") else value


def amounts_after(text: str, start: int, count: int) -> list[float]:
    """Take `count` amounts in turn from `start` (skipping percentages, footnote markers, etc.)."""
    out = []
    for token in text[start:].split():
        if AMOUNT_RE.fullmatch(token) and not FOOTNOTE_RE.fullmatch(token):
            out.append(to_number(token))
            if len(out) == count:
                break
    return out


class Pages:
    """Extract the text of the PDF pages on demand (whitespace collapsed to single spaces); pages not needed are not extracted."""

    def __init__(self, pdf: bytes):
        self._pages = PdfReader(io.BytesIO(pdf)).pages
        self._text: dict[int, str] = {}

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, i: int) -> str:
        if i not in self._text:
            self._text[i] = " ".join((self._pages[i].extract_text() or "").split())
        return self._text[i]


def parse_summary_table(text: str) -> tuple[str, dict[str, list[float]]]:
    """From the highlights table on page 1 extract EPS, Non-IFRS EPS, revenue and net profit attributable to owners; returns (currency, values of each row)."""
    text = " ".join(text.split())
    values: dict[str, list[float]] = {}
    currency = None
    for m in EPS_RE.finditer(text):
        key = "non_ifrs_eps" if m.group("non_ifrs") else "eps"
        if key in values:
            continue
        values[key] = [to_number(t) for t in m.group("body").split() if NUMBER_RE.fullmatch(t)]
        currency = currency or CURRENCY_CODES.get(m.group("ccy"), m.group("ccy"))
    if "eps" not in values:
        raise ValueError("The EPS row was not found")

    count = len(values["eps"])
    for key, pattern in (("revenue", REVENUE_RE), ("profit", PROFIT_RE)):
        m = pattern.search(text)
        if m:
            values[key] = amounts_after(text, m.end(), count)
    return currency, values


def parse_equity(text: str) -> list[float] | None:
    """From the balance sheet extract equity attributable to owners (end of this period, end of last year)."""
    m = EQUITY_RE.search(" ".join(text.split()))
    if not m:
        return None
    tokens = [t for t in m.group("body").split() if AMOUNT_RE.fullmatch(t)]
    return [to_number(t) for t in tokens[-2:]] if len(tokens) >= 2 else None


def parse_highlights(pages: Pages) -> tuple[str, dict[str, list[float]]]:
    """Highlights table on page 1 + balance sheet (Tencent)."""
    currency, values = parse_summary_table(pages[0])
    for i in range(1, len(pages)):
        equity = parse_equity(pages[i])
        if equity:
            values["equity"] = equity
            break
    for i in range(len(pages)):
        if m := WA_SHARES_RE.search(" ".join(pages[i].split()) + " "):
            values["wa_shares"] = [to_number(t) for t in m.group(1).split()]  # keep all values first; parse_announcement picks the two of the matching period
            break
    return currency, values


def first_profit_match(text: str) -> re.Match | None:
    """Net profit attributable to owners row: only the one immediately preceded by the "Profit for the period/year" label counts."""
    for m in PROFIT_STMT_RE.finditer(text):
        if PROFIT_LABEL_RE.search(text[max(0, m.start() - 45) : m.start()]):
            return m
    return None


def parse_statements(pages: Pages) -> tuple[str, dict[str, list[float]]]:
    """Consolidated income statement and balance sheet (Sunny Optical). Amounts are converted to millions, EPS to yuan.

    In the income statement, next to the net profit attributable to owners there is a "total comprehensive income" row written the same way, and in the balance sheet
    "equity attributable to owners" ends with the same words. So net profit must be inside the income statement area (after the revenue row, before the EPS row)
    and be preceded by the label "Profit for the period/year". If it is not found an error is raised; another row is never taken by mistake.
    """
    start = next((i for i in range(len(pages)) if REVENUE_STMT_RE.search(" ".join(pages[i].split()))), None)
    if start is None:
        raise ValueError("The consolidated income statement was not found")

    values: dict[str, list[float]] = {}
    currency = None
    scale = None  # factor converting amounts to millions

    def amounts(m: re.Match) -> list[float]:
        if scale is None:
            raise ValueError("Cannot find the unit of the amounts")
        return [to_number(m.group("a")) * scale, to_number(m.group("b")) * scale]

    for i in range(start, len(pages)):
        text = " ".join(pages[i].split())
        unit = UNIT_RE.search(text)
        if unit:  # unit in the table header; when the balance sheet spans pages, later pages reuse the earlier unit
            scale = 0.001 if unit.group("unit") == "000" else 1.0

        eps_match = EPS_STMT_RE.search(text) if "eps" not in values else None
        if "revenue" not in values and (m := REVENUE_STMT_RE.search(text)):
            values["revenue"] = amounts(m)
        if "profit" not in values and "eps" not in values:  # only look before the EPS row
            if m := first_profit_match(text[: eps_match.start()] if eps_match else text):
                values["profit"] = amounts(m)
        if eps_match:
            per_unit = 0.01 if eps_match.group("cents") else 1.0
            values["eps"] = [to_number(eps_match.group("a")) * per_unit, to_number(eps_match.group("b")) * per_unit]
            currency = CURRENCY_CODES[eps_match.group("ccy").upper()]
        if "equity" not in values and "eps" in values and (m := EQUITY_STMT_RE.search(text)):
            values["equity"] = amounts(m)  # the balance sheet comes after the income statement
        if {"revenue", "profit", "equity", "eps"} <= values.keys():
            break
    missing = {"revenue", "profit", "equity", "eps"} - values.keys()
    if missing:
        raise ValueError(f"Not found: {sorted(missing)}")
    for i in range(len(pages)):
        if m := WA_SHARES_STMT_RE.search(" ".join(pages[i].split())):
            values["wa_shares"] = [to_number(m.group("a")) / 1000, to_number(m.group("b")) / 1000]  # thousands of shares -> millions
            break
    return currency, values


FORMATS = {"highlights": parse_highlights, "statements": parse_statements}  # HKEX PDF announcements
FILING_FORMATS = {*FORMATS, "press_release", "us_press_release", "us_gaap_statements", "kifrs_statements", "cn_report"}  # plus US HTML press releases (6-K of JD; 8-K of US companies), Samsung English financial statements and A-share reports


def parse_announcement(filing: Filing, fmt: str = "highlights", market: Market = HK) -> Announcement:
    """Parse one earnings filing; values stay as in the filing (amounts converted to millions, EPS to yuan; not adjusted for splits)."""
    if fmt == "cn_report":  # A-shares: periodic report or express report (preliminary)
        parsed = parse_cn_express(filing.title, filing.content) if "express" in filing.title else parse_cn_report(filing.title, filing.content)
        return Announcement(
            parsed.period, filing.published, filing.url, parsed.currency, parsed.quarterly, parsed.values, market.key,
            filing.provisional, "", "period-end total shares", filing.note,
        )
    if fmt in ("press_release", "us_press_release", "us_gaap_statements", "kifrs_statements"):  # the parse result carries its own reporting period and whether it is a single quarter
        if fmt == "press_release":
            parsed = parse_press_release(filing.content)
        elif fmt in ("us_press_release", "us_gaap_statements"):
            parsed = parse_us_release(filing.content)
        elif filing.provisional:
            parsed = parse_kifrs_deck(filing.title, filing.content, filing.attachments["previous_bs"])
        else:
            parsed = parse_kifrs(filing.title, filing.content, filing.attachments["bs"], filing.attachments.get("note"))
        basis = "full year" if parsed.period.quarter == 4 and not parsed.quarterly else ""
        return Announcement(
            parsed.period, filing.published, filing.url, parsed.currency, parsed.quarterly, parsed.values, market.key,
            filing.provisional, basis,
        )
    period = parse_period(filing.title)
    currency, values = FORMATS[fmt](Pages(filing.content))
    shares = values.pop("wa_shares", None)
    counts = {len(values[k]) for k in FLOWS if k in values}
    if counts == {3}:
        quarterly = True
    elif counts == {2}:
        quarterly = period.quarter == 1  # the "cumulative" figure of a Q1 filing is the single quarter
    else:
        raise ValueError(f"{filing.url}: unexpected number of values per row: {values}")
    basis = ""
    if shares is not None:
        # Tencent's note has two numbers each for three months and cumulative (this period, same period last year): take the three-month ones, matching this quarter;
        # only two numbers means Q1 (three months) or the full year (the Q4 filing does not list Q4 shares separately)
        basis = "full year" if period.quarter == 4 and (len(shares) == 2 or fmt == "statements") else ""
        values["wa_shares"] = shares[:2]
    return Announcement(period, filing.published, filing.url, currency, quarterly, values, market.key, False, basis)


def split_factor(published: pd.Timestamp, splits: pd.Series) -> float:
    """The factor by which per-share data in the filing must be divided for splits that happened after the filing was published."""
    if splits.empty:
        return 1.0
    after = splits[splits.index > published.normalize()]
    return float(after.prod()) if len(after) else 1.0


def adjust_for_splits(announcements: list[Announcement], splits: pd.Series) -> list[Announcement]:
    """Convert per-share data to the current share basis (consistent with split-adjusted prices); amounts are not affected by splits."""
    out = []
    for a in announcements:
        factor = split_factor(a.published, splits)
        values = {
            k: [v / factor for v in vs] if k in PER_SHARE else [v * factor for v in vs] if k in SHARE_COUNTS else vs
            for k, vs in a.values.items()
        }
        out.append(replace(a, values=values))
    return out


ANNOUNCEMENT_COLUMNS = ["published", "period", "table", "currency", *SERIES, "url", "provisional", "shares_basis", "shares_label", "note", "parser_version"]


def announcements_to_frame(announcements: list[Announcement], parser_version: int) -> pd.DataFrame:
    """Storage format of parse results: one row per filing, values of a row joined with semicolons (numbers as in the filing)."""
    rows = [
        {
            "published": f"{a.published:%Y-%m-%d %H:%M}",
            "period": str(a.period),
            "table": "quarterly" if a.is_quarterly_table else "cumulative",
            "currency": a.currency,
            **{s: ";".join(f"{v:.10g}" for v in a.values[s]) if s in a.values else "" for s in SERIES},
            "url": a.url,
            "provisional": int(a.provisional),
            "shares_basis": a.shares_basis,
            "shares_label": a.shares_label,
            "note": a.note,
            "parser_version": parser_version,
        }
        for a in sorted(announcements, key=lambda a: a.published)
    ]
    return pd.DataFrame(rows, columns=ANNOUNCEMENT_COLUMNS)


def announcements_from_frame(df: pd.DataFrame, market: Market = HK) -> list[Announcement]:
    out = []
    for r in df.fillna("").to_dict("records"):
        out.append(
            Announcement(
                period=pd.Period(r["period"], freq="Q"),
                published=pd.Timestamp(r["published"]),
                url=r["url"],
                currency=r["currency"],
                is_quarterly_table=r["table"] == "quarterly",
                values={s: [float(v) for v in str(r[s]).split(";")] for s in SERIES if str(r.get(s, ""))},
                market=market.key,
                provisional=str(r.get("provisional", "")) == "1",
                shares_basis=str(r.get("shares_basis", "")),
                shares_label=str(r.get("shares_label", "")) or "basic EPS weighted average shares",
                note=str(r.get("note", "")),
            )
        )
    return out


def implied_shares(quarters: dict[str, dict[pd.Period, QuarterValue]], p: pd.Period) -> float | None:
    """Weighted average shares of the period (millions) = net profit attributable to owners of the period / basic EPS of the period."""
    profit, eps = quarters["profit"].get(p), quarters["eps"].get(p)
    if profit is None or eps is None or eps.value == 0:
        return None
    return profit.value / eps.value


@dataclass
class EarningsHistory:
    currency: str
    quarters: dict[str, dict[pd.Period, QuarterValue]]  # series -> period -> single-period value (equity: period-end value)
    ttm: pd.DataFrame  # data available after each filing, columns see TTM_COLUMNS
    announcements: list[Announcement]
    periods_per_year: int = 4  # earnings periods published per year: 4 (quarterly), 2 (half-yearly), 1 (annual)
    ttm_detail: list[dict] = field(default_factory=list)  # row by row matching ttm: how each item was calculated (see audit.py)
    shares_basis: dict[pd.Period, str] = field(default_factory=dict)  # period the disclosed weighted average shares of each period refer to (empty = this period, "full year")

    @property
    def step(self) -> int:
        """How many quarters lie between adjacent periods."""
        return 4 // self.periods_per_year

    def discrepancies(self) -> list[str]:
        out = []
        for series, quarters in self.quarters.items():
            fmt = "{:.3f}" if series in PER_SHARE else "{:,.0f}"
            for p, q in sorted(quarters.items()):
                if q.max_deviation > tolerance(series, q.value):
                    vals = ", ".join(fmt.format(o.value) + (" (derived from cumulative figures)" if o.derived else "") for o in q.observations)
                    out.append(
                        f"{SERIES_LABELS[series]} {period_label(p, self.periods_per_year)} differs between filings (probably a restatement) [{vals}]; "
                        f"using the first published value {fmt.format(q.value)}"
                    )
        for a in self.announcements:
            if a.provisional:
                out.append(f"The {period_label(a.period, self.periods_per_year)} data {PROVISIONAL_NOTES.get(a.market, PROVISIONAL_NOTES['*'])}")
        shares = {p: s for p in sorted(self.quarters["eps"]) if (s := implied_shares(self.quarters, p))}
        periods = list(shares)
        label = lambda p: period_label(p, self.periods_per_year)  # noqa: E731
        for prev, cur in zip(periods, periods[1:]):
            if cur - prev == self.step and abs(shares[cur] / shares[prev] - 1) > SHARES_CHANGE_LIMIT:
                out.append(
                    f"Implied shares {label(prev)} {shares[prev]:,.0f} -> {label(cur)} {shares[cur]:,.0f} million changed by more than 5%; please check"
                )
        return out

    def periods_frame(self, trading_days: pd.DatetimeIndex | None = None) -> pd.DataFrame:
        """Per-period detail (quarterly or half-yearly). When trading days are given, also lists the trading day each value actually starts to be used."""
        rows = []
        for p in sorted(set().union(*self.quarters.values())):
            row = {"period": period_label(p, self.periods_per_year), "currency": self.currency}
            for series in SERIES:
                q = self.quarters[series].get(p)
                row[series] = round(q.value, 4) if q else None
                row[f"{series}_published"] = f"{q.published:%Y-%m-%d %H:%M}" if q else None
                row[f"{series}_effective"] = effective_day(q.available, trading_days) if q else None
                row[f"{series}_sources"] = len(q.observations) if q else 0
                row[f"{series}_max_deviation"] = round(q.max_deviation, 4) if q else None
            shares = implied_shares(self.quarters, p)
            row["shares"] = round(shares, 1) if shares else None
            row["first_source"] = next(
                (q.observations[0].url for s in SERIES if (q := self.quarters[s].get(p))), None
            )
            rows.append(row)
        return pd.DataFrame(rows)

    def ttm_frame(self, trading_days: pd.DatetimeIndex | None = None) -> pd.DataFrame:
        """The data available after each filing, with the calculation: values of the periods the TTM covers, formulas and intermediate quantities."""
        df = self.ttm.copy()
        df.insert(3, "effective_date", [effective_day(d, trading_days) for d in df["available_date"]])
        slots = 4
        extra = {"provisional": [int(r["provisional"]) for r in self.ttm_detail]}
        for k in range(slots):
            extra[f"period_{k + 1}"] = [r["periods"][k] if k < len(r["periods"]) else None for r in self.ttm_detail]
        for series in ("eps", "non_ifrs_eps", "revenue"):
            for k in range(slots):
                extra[f"{series}_{k + 1}"] = [
                    r[series][k].value if r[series] and k < len(r[series]) else None for r in self.ttm_detail
                ]
        extra["profit_latest"] = [r["profit"] for r in self.ttm_detail]
        extra["eps_latest"] = [r["eps_latest"] for r in self.ttm_detail]
        extra["eps_ttm_formula"] = [sum_formula(r["eps"], r["eps_ttm"]) for r in self.ttm_detail]
        extra["non_ifrs_eps_ttm_formula"] = [sum_formula(r["non_ifrs_eps"], r["non_ifrs_eps_ttm"]) for r in self.ttm_detail]
        extra["revenue_ttm_formula"] = [sum_formula(r["revenue"], r["revenue_ttm"]) for r in self.ttm_detail]
        extra["shares_formula"] = [shares_formula(r) for r in self.ttm_detail]
        extra["shares_source"] = [{"reported": "reported", "derived": "derived from the full-year figure", "implied": "implied (profit / EPS)"}[r["shares_source"]] for r in self.ttm_detail]
        extra["shares_implied"] = [r["shares_implied"] for r in self.ttm_detail]
        extra["revenue_per_share_formula"] = [
            _division(r["revenue_ttm"], r["shares"], r["revenue_per_share_ttm"]) for r in self.ttm_detail
        ]
        extra["book_value_per_share_formula"] = [_division(r["equity"], r["shares"], r["book_value_per_share"]) for r in self.ttm_detail]
        extra["published_note"] = [r["note"] for r in self.ttm_detail]
        extra["split_note"] = [
            "; ".join(split_notes(r["eps"]) + split_notes(r["non_ifrs_eps"])) for r in self.ttm_detail
        ]
        df = pd.concat([df.reset_index(drop=True), pd.DataFrame(extra)], axis=1)
        return df[[c for c in TTM_FRAME_ORDER if c in df]]


def _division(numerator: float | None, denominator: float | None, result: float | None) -> str:
    if numerator is None or not denominator or result is None:
        return ""
    return f"{num(numerator)} / {num(denominator)} = {num(result)}"


def effective_day(available: pd.Timestamp, trading_days: pd.DatetimeIndex | None):
    """The trading day (date) it actually starts to be used; empty when the price data does not have that day yet (market not yet open) or no trading days were given."""
    if trading_days is None:
        return None
    day = effective_trading_day(available, trading_days)
    return day.date() if day is not None else None


def infer_periods_per_year(periods: list[pd.Period]) -> int:
    """Periods published per year: a filing for Q1 or Q3 means quarterly reporting; only Q2 (interim) means half-yearly."""
    quarters = {p.quarter for p in periods}
    if quarters & {1, 3}:
        return 4
    return 2 if 2 in quarters else 1


def build_history(parsed: list[Announcement], splits: pd.Series | None = None) -> EarningsHistory:
    announcements: dict[pd.Period, Announcement] = {}
    for a in sorted(parsed, key=lambda x: x.available):
        announcements.setdefault(a.period, a)  # for duplicate filings of the same reporting period only the earliest is taken

    currencies = {a.currency for a in announcements.values()}
    if len(currencies) != 1:
        raise ValueError(f"The filings disagree on the EPS currency: {currencies}")
    per_year = infer_periods_per_year(list(announcements))
    step = 4 // per_year

    quarters: dict[str, dict[pd.Period, QuarterValue]] = {s: {} for s in SERIES}
    ytd: dict[str, dict[pd.Period, float]] = {s: {} for s in FLOWS}
    ttm_rows, details = [], []
    shares_basis: dict[pd.Period, str] = {}

    def record(series: str, period: pd.Period, value: float, a: Announcement, derived: bool) -> None:
        q = quarters[series].setdefault(period, QuarterValue(value, a.available, a.published))
        q.observations.append(Observation(value, a.url, derived))

    for a in sorted(announcements.values(), key=lambda x: x.available):
        for series, vals in a.values.items():
            touched: list[tuple[pd.Period, float]] = []  # the year-to-date cumulative figures in this filing
            if series in BALANCES:
                prior_year_end = pd.Period(year=a.period.year - 1, quarter=4, freq="Q")
                for period, value in zip([a.period, prior_year_end], vals):
                    record(series, period, value, a, derived=False)
            elif series in SHARE_COUNTS:  # shares disclosed in the filing: the value of that period itself, cannot be obtained by subtracting cumulative figures
                for period, value in zip([a.period, a.period - 4, a.period - 1], vals):
                    record(series, period, value, a, derived=False)
                    shares_basis.setdefault(period, a.shares_basis)
            elif a.is_quarterly_table:
                for period, value in zip([a.period, a.period - 4, a.period - 1], vals):
                    record(series, period, value, a, derived=False)
            else:
                for period, value in zip([a.period, a.period - 4], vals):
                    ytd[series].setdefault(period, value)
                    touched.append((period, value))
            if series in FLOWS:
                _derive_from_ytd(series, quarters[series], ytd[series], a, record, step)
                _cross_check_ytd(quarters[series], touched, a, step)

        row, detail = _ttm_row(a, quarters, per_year, splits if splits is not None else pd.Series(dtype=float), shares_basis)
        ttm_rows.append(row)
        details.append(detail)

    ttm = pd.DataFrame(ttm_rows, columns=TTM_COLUMNS)
    return EarningsHistory(currencies.pop(), quarters, ttm, list(announcements.values()), per_year, details, shares_basis)


def _ttm_row(
    a: Announcement, quarters: dict[str, dict[pd.Period, QuarterValue]], per_year: int, splits: pd.Series, shares_basis: dict[pd.Period, str]
) -> tuple[dict, dict]:
    """The data available after this filing is published: each TTM (sum of the periods in the last year), shares, revenue per share, book value per share.

    Returns (row of values, calculation). The calculation records which values of which periods add up to each TTM, for checking.
    """
    last_year = [a.period - k * (4 // per_year) for k in range(per_year)]
    periods = list(reversed(last_year))  # oldest to newest

    def components(series: str) -> list[Component] | None:
        qs = quarters[series]
        if not all(p in qs for p in periods):
            return None
        out = []
        for p in periods:
            q = qs[p]
            derived = q.observations[0].derived
            factor = split_factor(q.published, splits) if series in PER_SHARE and not derived else 1.0
            out.append(Component(period_label(p, per_year), q.value, derived, q.value * factor if factor != 1.0 else None))
        return out

    def total(comps: list[Component] | None) -> float | None:
        return sum(c.value for c in comps) if comps else None

    def rounded(value: float | None, digits: int = 4) -> float | None:
        return round(value, digits) if value is not None else None

    eps, non_ifrs, revenue = components("eps"), components("non_ifrs_eps"), components("revenue")
    implied = implied_shares(quarters, a.period)
    reported = quarters["wa_shares"].get(a.period)
    basis = shares_basis.get(a.period, "") if reported else ""
    shares, source, parts = (reported.value, "reported", None) if reported else (implied, "implied", None)
    if reported and basis:
        # the filing only discloses the full year: the full-year figure is the day-weighted average of the quarters, so this period (Q4 or the second half) = full year x days in year - earlier periods x their days, divided by the days of this period
        parts = _fiscal_year_parts(a.period, reported.value, quarters["wa_shares"], shares_basis, 4 // per_year)
        if parts:
            shares, source = parts["shares"], "derived"
            for x in parts["parts"]:
                x["period"] = period_label(x["period"], per_year)
        else:
            shares, source, basis = implied, "implied", ""  # the earlier periods have no disclosed shares either: fall back to implied
    revenue_ttm = total(revenue)
    equity = quarters["equity"].get(a.period)
    row = {
        "report_date": a.period.end_time.normalize(),
        "published": f"{a.published:%Y-%m-%d %H:%M}",
        "available_date": a.available,
        "eps_ttm": rounded(total(eps)),
        "non_ifrs_eps_ttm": rounded(total(non_ifrs)),
        "revenue_ttm": rounded(revenue_ttm, 1),
        "shares": rounded(shares, 3),
        "revenue_per_share_ttm": rounded(revenue_ttm / shares) if revenue_ttm is not None and shares else None,
        "equity": rounded(equity.value, 1) if equity else None,
        "book_value_per_share": rounded(equity.value / shares) if equity and shares else None,
    }
    profit, eps_now = quarters["profit"].get(a.period), quarters["eps"].get(a.period)
    detail = {
        "period_label": period_label(a.period, per_year),
        "published": f"{a.published:%Y-%m-%d %H:%M}",
        "provisional": a.provisional,
        "periods": [period_label(p, per_year) for p in periods],
        "eps": eps,
        "non_ifrs_eps": non_ifrs,
        "revenue": revenue,
        "eps_ttm": row["eps_ttm"],
        "non_ifrs_eps_ttm": row["non_ifrs_eps_ttm"],
        "revenue_ttm": row["revenue_ttm"],
        "profit": profit.value if profit else None,
        "eps_latest": eps_now.value if eps_now else None,
        "shares": row["shares"],
        "shares_label": a.shares_label,
        "note": a.note,
        "shares_source": source,
        "shares_basis": basis,
        "shares_parts": parts,
        "shares_implied": implied,
        "equity": row["equity"],
        "revenue_per_share_ttm": row["revenue_per_share_ttm"],
        "book_value_per_share": row["book_value_per_share"],
    }
    return row, detail


def _days(p: pd.Period, step: int) -> int:
    """Days of a period; step is the number of quarters in a period (a half-year period is two quarters)."""
    return (p.end_time.normalize() - (p - (step - 1)).start_time.normalize()).days + 1


def _fiscal_year_parts(
    p: pd.Period, full_year: float, shares: dict[pd.Period, QuarterValue], basis: dict[pd.Period, str], step: int
) -> dict | None:
    """Full-year weighted average shares -> weighted average shares of this period (the last period of the year). Needs the shares disclosed for the earlier periods of the year (on this period's own basis)."""
    earlier = _earlier_periods(p, step)
    if not earlier or any(e not in shares or basis.get(e, "") for e in earlier):
        return None
    year_days = (pd.Timestamp(year=p.year, month=12, day=31) - pd.Timestamp(year=p.year, month=1, day=1)).days + 1
    parts = [{"period": e, "shares": shares[e].value, "days": _days(e, step)} for e in sorted(earlier)]
    days = _days(p, step)
    value = (full_year * year_days - sum(x["shares"] * x["days"] for x in parts)) / days
    return {"shares": value, "full_year": full_year, "year_days": year_days, "days": days, "parts": parts}


def _earlier_periods(p: pd.Period, step: int) -> list[pd.Period]:
    """The periods of the same year before p. step is the number of quarters between adjacent periods."""
    return [p - j * step for j in range(1, p.quarter // step)]


def _derive_from_ytd(series, quarters, ytd, a, record, step: int) -> None:
    """Derive a single-period value not yet known from year-to-date cumulative figures: this period = cumulative(this period) - cumulative(previous period), or - the sum of the earlier periods of the year.

    For quarterly reporters the periods within a year are quarters; for half-yearly reporters the cumulative first-half figure is the first half itself,
    and the second half = full year - first half.
    """
    changed = True
    while changed:
        changed = False
        for p, total in ytd.items():
            if p in quarters:
                continue
            earlier = _earlier_periods(p, step)
            if not earlier:
                value = total
            elif (p - step) in ytd:
                value = total - ytd[p - step]
            elif all(e in quarters for e in earlier):
                value = total - sum(quarters[e].value for e in earlier)
            else:
                continue
            record(series, p, value, a, derived=True)
            changed = True


def _cross_check_ytd(quarters, touched, a, step: int) -> None:
    """The cumulative figures in this filing (including the same period last year) can also give the values of periods; used as another source of the existing values, for cross-checking."""
    for p, total in touched:
        q = quarters.get(p)
        earlier = _earlier_periods(p, step)
        if q is None or any(o.url == a.url for o in q.observations) or not all(e in quarters for e in earlier):
            continue
        q.observations.append(Observation(total - sum(quarters[e].value for e in earlier), a.url, True))
