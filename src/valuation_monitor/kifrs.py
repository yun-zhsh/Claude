"""Extract earnings data from Samsung Electronics' English consolidated financial statements (K-IFRS).

Two files are read for each quarter: the consolidated statement of income (soi) and of financial position (bs).
  - Income statement columns: KRW amounts of this period and the same period last year, then the same two columns converted to USD, then the year-to-date columns.
    For Q1-Q3 the first two columns of "three months" are used (this quarter, same quarter last year); for Q4 the reporting period is the full year and the first two
    columns (this year, last year) are used as cumulative figures; the single quarter is obtained by the program as full year minus the first three quarters.
  - Revenue is Revenue, net profit is Profit attributable to owners of the parent, EPS is basic EPS (ordinary shares, KRW);
    amounts are in millions of KRW.
  - Equity attributable to owners of the parent = Total equity - Non-controlling interests, for the end of this period and the end of last year.
Samsung has ordinary and preferred shares and net profit includes the part attributable to preferred shares, so "net profit / basic EPS" implies the combined weighted average shares of ordinary plus preferred shares.
"""

from __future__ import annotations

import io
import re
import sys

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.parsed import Parsed

AMT = r"\(?\d{1,3}(?:,\d{3})+\)?"  # amounts with thousands separators; note numbers (e.g. 27, 18, 19) do not count
EPS_AMT = r"\(?\d[\d,]*\)?"
TITLE_RE = re.compile(r"(?P<q>[1-4])Q (?P<year>20\d\d)")
PERIOD_END = {1: "March 31", 2: "June 30", 3: "September 30", 4: "December 31"}
REVENUE_RE = re.compile(rf"\bRevenue(?: [\d, ]{{1,8}}?)? (?P<a>{AMT}) (?P<b>{AMT})")
PROFIT_RE = re.compile(rf"Profit attributable to:? (?:owners of the (?:parent(?: company)?|Company)\s+)?(?P<a>{AMT}) (?P<b>{AMT})", re.I)
EPS_RE = re.compile(rf"Earnings per share.{{0,200}}?- Basic (?P<a>{EPS_AMT}) (?P<b>{EPS_AMT})", re.I)
TOTAL_EQUITY_RE = re.compile(rf"Total equity (?P<a>{AMT}) (?P<b>{AMT})")
NCI_RE = re.compile(rf"Non-controlling interests(?: \d{{1,2}})? (?P<a>{AMT}) (?P<b>{AMT})", re.I)


def read_text_of_page(page) -> str:
    return clean_text(page.extract_text() or "")


def read_text(pdf: bytes) -> str:
    return clean_text(" ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages))


def clean_text(text: str) -> str:
    text = " ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).split())  # a few files contain control characters
    text = re.sub(r"(\d), (\d{3})\b", r"\1,\2", text)  # PDF text extraction inserts a space after thousands separators, e.g. "44, 260,956"
    # it also splits the last digit group, e.g. "5,969,78 3": merge only when the first part has fewer than three digits and together they make exactly three
    return re.sub(r"(?<![\d,])(\d{1,3}(?:,\d{3})*,)(\d{1,2}) (\d{1,2})(?![\d,])", lambda m: m.group(1) + m.group(2) + m.group(3) if len(m.group(2) + m.group(3)) == 3 else m.group(0), text)


def to_number(token: str) -> float:
    value = float(token.strip("()").replace(",", ""))
    return -value if token.startswith("(") else value


def pair(regex: re.Pattern, text: str, name: str) -> list[float]:
    m = regex.search(text)
    if not m:
        raise ValueError(f"Not found in the statements: {name}")
    return [to_number(m.group("a")), to_number(m.group("b"))]


# EPS note (Earnings per Share in the Notes): weighted average shares, in thousands of shares.
# In the Q1, Q2, Q3 notes each year has two columns (three months, cumulative), four numbers in all: this year three months, this year cumulative, last year three months, last year cumulative;
# Q1 and the full year have only two numbers, this year and last year.
SHARE_ROW = r"Weighted-average number of {kind} shares outstanding(?: \(in thousands\))? (?P<n>(?:[\d,]+ ){{1,4}})"
COMMON_SHARES_RE = re.compile(SHARE_ROW.format(kind="(?:ordinary|common)"))
PREFERRED_SHARES_RE = re.compile(SHARE_ROW.format(kind="(?:preferred|preference)"))


def eps_note_pages(note: bytes) -> bytes | None:
    """From the full note PDF keep only the pages with the Earnings per Share note (pages unchanged; the preferred-share part is often on the next page),
    used when saving the download, so that each quarter does not store hundreds of KB."""
    from pypdf import PdfWriter

    reader = PdfReader(io.BytesIO(note))
    texts = [read_text_of_page(page) for page in reader.pages]
    # what is needed is the table in the notes ("Weighted-average number of ordinary shares outstanding 5,767,030 ..."), not the text in the accounting policies
    first = next((i for i, t in enumerate(texts) if COMMON_SHARES_RE.search(t + " ")), None)
    if first is None:
        return None
    writer = PdfWriter()
    for i in range(first, min(first + 2, len(texts))):
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def current_and_prior(numbers: str) -> tuple[float, float] | None:
    values = [to_number(t) for t in numbers.split()]
    if len(values) == 4:
        return values[0], values[2]
    if len(values) == 2:
        return values[0], values[1]
    return None


def parse_weighted_shares(note: bytes) -> list[float] | None:
    """Basic EPS weighted average shares of ordinary plus preferred shares disclosed in the note (numbers as in the note, unit varies): this period, same period last year.

    The unit differs between years (thousands of shares, shares); the caller decides it by comparing with the magnitude of net profit / EPS.
    """
    text = read_text(note) + " "
    common = COMMON_SHARES_RE.search(text)
    if not common or not (c := current_and_prior(common.group("n"))):
        return None
    preferred = PREFERRED_SHARES_RE.search(text)
    p = current_and_prior(preferred.group("n")) if preferred else (0.0, 0.0)
    if p is None:
        return None
    return [c[0] + p[0], c[1] + p[1]]


def scale_to_millions(raw: list[float], implied_millions: float) -> list[float] | None:
    """Are the shares in the note in thousands or in single shares? Use the unit whose conversion is within 5% of "net profit attributable to owners / basic EPS"."""
    for factor in (1e-6, 1e-3, 1.0):
        if abs(raw[0] * factor / implied_millions - 1) < 0.05:
            return [v * factor for v in raw]
    return None


def parse_kifrs(title: str, soi: bytes, bs: bytes, note: bytes | None = None) -> Parsed:
    m = TITLE_RE.search(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    quarter, year = int(m.group("q")), int(m.group("year"))
    income, balance = read_text(soi), read_text(bs)
    if f"{PERIOD_END[quarter]}, " not in income and f"{PERIOD_END[quarter]}," not in income.replace(" ,", ","):
        raise ValueError(f"The income statement does not mention {PERIOD_END[quarter]}, so it does not match the reporting period {quarter}Q {year} in the title")

    total, nci = pair(TOTAL_EQUITY_RE, balance, "Total equity"), pair(NCI_RE, balance, "Non-controlling interests")
    values = {
        "revenue": pair(REVENUE_RE, income, "Revenue"),
        "profit": pair(PROFIT_RE, income, "Profit attributable to owners of the parent"),
        "eps": pair(EPS_RE, income, "Basic EPS"),
        "equity": [t - n for t, n in zip(total, nci)],
    }
    if note is not None and (raw := parse_weighted_shares(note)):
        scaled = scale_to_millions(raw, values["profit"][0] / values["eps"][0])
        if scaled is None:
            print(f"{title}: the share count in the notes ({raw[0]:,.0f}) is not of the order of profit / EPS, so its unit cannot be determined; shares are implied instead", file=sys.stderr)
        else:
            values["wa_shares"] = scaled
    return Parsed("KRW", values, pd.Period(year=year, quarter=quarter, freq="Q"), quarterly=quarter != 4)


# Earnings Call material, layout since 2026: three columns: same quarter last year, previous quarter, this quarter
DECK_PCT_COLUMNS = r"(?P<a>[\d.]+) [\d.]+% (?P<b>[\d.]+) [\d.]+% (?P<c>[\d.]+) [\d.]+%"
DECK_SALES_RE = re.compile(rf"\bSales {DECK_PCT_COLUMNS}")
DECK_PROFIT_RE = re.compile(rf"Profit ?attributable to owners of the parent {DECK_PCT_COLUMNS}")
DECK_EPS_RE = re.compile(r"EPS ?\(KRW\) ?※ ?Common stock (?P<a>[\d,]+) (?P<b>[\d,]+) (?P<c>[\d,]+)")
DECK_EQUITY_RE = re.compile(r"Shareholder equity (?P<a>[\d,]+\.\d) (?P<b>[\d,]+\.\d) (?P<c>[\d,]+\.\d)")
# The Q4 material has two more full-year columns: same quarter last year, previous quarter, this quarter, last full year, this full year (only revenue and net profit carry a "share %")
FY_PCT = r"(?P<q4y>[\d.]+) (?P<q3>[\d.]+) [\d.]+% (?P<q4>[\d.]+) [\d.]+% (?P<fy0>[\d.]+) [\d.]+% (?P<fy1>[\d.]+) [\d.]+%"
DECK_FY_SALES_RE = re.compile(rf"\bSales {FY_PCT}")
DECK_FY_PROFIT_RE = re.compile(rf"Profit ?attributable to owners of the parent {FY_PCT}")
DECK_FY_EPS_RE = re.compile(
    r"(?:EPS ?\(KRW\) ?※ ?Common stock|Earnings per share ?\(KRW\)) (?P<q4y>[\d,]+) (?P<q3>[\d,]+) (?P<q4>[\d,]+) (?P<fy0>[\d,]+) (?P<fy1>[\d,]+)"
)
TRILLION = 1_000_000  # millions of KRW
BILLION = 1_000


def deck_columns(regex: re.Pattern, text: str, name: str) -> list[float]:
    """A row of the call material, returns [this quarter, same quarter last year, previous quarter]."""
    m = regex.search(text)
    if not m:
        raise ValueError(f"{name} was not found in the earnings call presentation (the layout may have changed)")
    yoy, qoq, cur = (float(m.group(k).replace(",", "")) for k in "abc")
    return [cur, yoy, qoq]


def fiscal_year_columns(regex: re.Pattern, text: str, name: str) -> list[float]:
    """A row of the Q4 material, returns [this full year, last full year]."""
    m = regex.search(text)
    if not m:
        raise ValueError(f"{name} was not found in the Q4 earnings call presentation (the layout may have changed)")
    return [float(m.group("fy1").replace(",", "")), float(m.group("fy0").replace(",", ""))]


def parse_kifrs_deck(title: str, deck: bytes, previous_bs: bytes) -> Parsed:
    """The quarter's results in the earnings call material (before review; used for now and replaced by the English statements when they are out).

    Revenue and net profit are only precise to 0.1 trillion KRW; EPS is the exact KRW figure. The equity in the material includes non-controlling interests;
    equity attributable to the parent is estimated with the "attributable / total" ratio of the previous quarter's statements (non-controlling interests are only 2-3%, so it changes little).
    Q4 is handled like the annual report as full-year cumulative figures (this year, last year), and the single quarter is obtained by the program as full year minus the first three quarters,
    so the Q4 TTM EPS equals the full-year EPS of the annual report and the numbers do not jump once the statements are out.
    """
    m = TITLE_RE.search(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    quarter, year = int(m.group("q")), int(m.group("year"))
    text = read_text(deck)
    if f"{quarter}Q {year} Results" not in text:
        raise ValueError(f"The presentation is not for {quarter}Q {year}, or its layout has changed")

    previous = read_text(previous_bs)
    total_prev, nci_prev = pair(TOTAL_EQUITY_RE, previous, "Total equity")[0], pair(NCI_RE, previous, "Non-controlling interests")[0]
    equity_total = deck_columns(DECK_EQUITY_RE, text, "Shareholder equity")[0] * BILLION
    equity = [equity_total * (total_prev - nci_prev) / total_prev]
    period = pd.Period(year=year, quarter=quarter, freq="Q")
    if quarter == 4:
        values = {
            "revenue": [v * TRILLION for v in fiscal_year_columns(DECK_FY_SALES_RE, text, "Sales")],
            "profit": [v * TRILLION for v in fiscal_year_columns(DECK_FY_PROFIT_RE, text, "Profit attributable to owners of the parent")],
            "eps": fiscal_year_columns(DECK_FY_EPS_RE, text, "EPS"),
            "equity": equity,
        }
        return Parsed("KRW", values, period, quarterly=False)
    values = {
        "revenue": [v * TRILLION for v in deck_columns(DECK_SALES_RE, text, "Sales")],
        "profit": [v * TRILLION for v in deck_columns(DECK_PROFIT_RE, text, "Profit attributable to owners of the parent")],
        "eps": deck_columns(DECK_EPS_RE, text, "EPS"),
        "equity": equity,
    }
    return Parsed("KRW", values, period, quarterly=True)
