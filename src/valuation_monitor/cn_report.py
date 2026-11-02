"""Extract earnings data from the Chinese periodic reports and express reports of A-share companies
(PDF originals; verified on all Hikvision reports from 2014 on).

The report type decides which numbers are used:
  Q1 report        consolidated income statement, current period and prior period (single quarter)
  Half-year report consolidated income statement, current and prior period (cumulative, first half)
  Q3 report        the single-quarter Q3 figures are only in the "key accounting data" table (its first column)
  Annual report    consolidated income statement, current and prior period (full year, cumulative)
  Express report   preliminary annual figures (not audited by the accounting firm), published before the annual
                   report: revenue, net profit, EPS, equity, share capital
Revenue is total operating revenue, net profit is the profit attributable to shareholders of the parent, EPS is
basic EPS; amounts are in RMB and converted to millions. Equity is the equity attributable to owners of the parent
on the balance sheet; the share count is the balance sheet's share capital (period-end total shares, par value
RMB 1, so shares = the share capital amount).
In every periodic report the first column of the key-data table must match the same item in the consolidated
statements; otherwise parsing fails.

The Chinese patterns live in cn_patterns.py; this module is language-neutral.
"""

from __future__ import annotations

import io
import re

import pandas as pd
from pypdf import PdfReader

from valuation_monitor import cn_patterns as cn
from valuation_monitor.parsed import Parsed

NUM = r"(-?[\d,]+(?:\.\d+)?|\(\s*[\d,]+(?:\.\d+)?\s*\))"
MILLION = 1e6
# Titles are English and built by the CNINFO provider: "2026 Q3 report", "2025 annual report", "2025 annual express report"
REPORT_TITLE_RE = re.compile(r"^(?P<year>\d{4}) (?P<kind>Q1|H1|Q3|annual) report$")
EXPRESS_TITLE_RE = re.compile(r"^(?P<year>\d{4}) (?P<kind>H1|annual) express report$")
QUARTER_OF = {"Q1": 1, "H1": 2, "Q3": 3, "annual": 4}


def clean_text(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(cn.CJK_GAP_RE, "", text)
    text = re.sub(r"(\d,\d{3}(?:,\d{3})*\.\d) (\d)(?![\d,])", r"\1\2", text)  # the last decimal digit gets split off, e.g. "158.1 1"
    # The last digit group of the integer part gets split, e.g. "9,335,806,1 14.00": merge only when the pieces add up to three digits
    return re.sub(
        r"(?<![\d,])(\d{1,3}(?:,\d{3})*,)(\d{1,2}) (\d{1,2})(?![\d,])",
        lambda m: m.group(1) + m.group(2) + m.group(3) if len(m.group(2) + m.group(3)) == 3 else m.group(0),
        text,
    )


def read_text(pdf: bytes) -> str:
    return clean_text(" ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages))


def to_number(token: str) -> float:
    token = token.strip()
    if token.startswith("("):
        return -float(token.strip("()").replace(",", ""))
    return float(token.replace(",", ""))


def row(text: str, name: str, label: str, count: int, start: int = 0, optional: str = "") -> tuple[list[float], int]:
    """The `count` numbers that follow `label` (a footnote marker or note number may sit in between).

    Returns (numbers, end position of the row in the text); raises if the row cannot be found.
    """
    m = re.compile(rf"(?:{label})\s*{optional}\s*{cn.NOTE}" + r"\s+".join([NUM] * count)).search(text, start)
    if not m:
        raise ValueError(f"Row not found in the report: {name}")
    return [to_number(g) for g in m.groups()], m.end()


def parse_cn_report(title: str, pdf: bytes) -> Parsed:
    m = REPORT_TITLE_RE.match(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    quarter = QUARTER_OF[m.group("kind")]
    period = pd.Period(year=int(m.group("year")), quarter=quarter, freq="Q")
    text = read_text(pdf)

    # Key accounting data table: the first column is the reporting period (the only source of the Q3 single quarter)
    key_start = text.find(cn.KEY_TABLE)
    if key_start < 0:
        raise ValueError("The key accounting data table was not found")
    key = {
        "revenue": row(text, "revenue (key table)", cn.KEY_REVENUE, 1, key_start, cn.FOOTNOTE)[0][0],
        "profit": row(text, "net profit (key table)", cn.KEY_PROFIT, 1, key_start, cn.FOOTNOTE)[0][0],
        "eps": row(text, "basic EPS (key table)", cn.KEY_EPS, 1, key_start, cn.FOOTNOTE)[0][0],
    }

    # Consolidated statements: current and prior period (the prior period as restated in this report)
    revenue, pos = row(text, "revenue", cn.INCOME_REVENUE, 2)
    profit, pos = row(text, "net profit", cn.INCOME_PROFIT, 2, pos)
    eps, _ = row(text, "basic EPS", cn.INCOME_EPS, 2, pos)
    values = {"revenue": [v / MILLION for v in revenue], "profit": [v / MILLION for v in profit], "eps": eps}

    equity, _ = row(text, "equity", cn.EQUITY, 2)
    share_capital, _ = row(text, "share capital", cn.SHARE_CAPITAL, 2)
    values["equity"] = [equity[0] / MILLION, equity[1] / MILLION]
    values["wa_shares"] = [share_capital[0] / MILLION]  # period-end total shares (millions)

    quarterly = quarter in (1, 3)
    if quarter == 3:
        # The Q3 consolidated income statement is year-to-date, so the single quarter comes from the key table
        values["revenue"] = [key["revenue"] / MILLION]
        values["profit"] = [key["profit"] / MILLION]
        values["eps"] = [key["eps"]]
    else:
        for name, scale in (("revenue", MILLION), ("profit", MILLION), ("eps", 1.0)):
            if abs(values[name][0] - key[name] / scale) > (1e-6 if scale > 1 else 0.0006) * max(1.0, abs(values[name][0])):
                raise ValueError(f"{name}: the key table says {key[name]} but the consolidated income statement says {values[name][0] * scale}")
    return Parsed("CNY", values, period, quarterly)


def parse_cn_express(title: str, pdf: bytes) -> Parsed:
    """Express report: preliminary annual (or half-year) figures. The numbers are cumulative (current, prior-year period)."""
    m = EXPRESS_TITLE_RE.match(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    quarter = 2 if m.group("kind") == "H1" else 4
    period = pd.Period(year=int(m.group("year")), quarter=quarter, freq="Q")
    text = read_text(pdf)
    revenue, _ = row(text, "revenue", cn.EXPRESS_REVENUE, 2, 0, cn.FOOTNOTE)
    profit, _ = row(text, "net profit", cn.EXPRESS_PROFIT, 2, 0, cn.FOOTNOTE)
    eps, _ = row(text, "basic EPS", cn.EXPRESS_EPS, 2, 0, cn.EXPRESS_EPS_UNIT + cn.FOOTNOTE)
    equity, _ = row(text, "equity", cn.EXPRESS_EQUITY, 2, 0, cn.FOOTNOTE)
    shares, _ = row(text, "share capital", cn.EXPRESS_SHARES, 2, 0, cn.FOOTNOTE)
    values = {
        "revenue": [v / MILLION for v in revenue],
        "profit": [v / MILLION for v in profit],
        "eps": eps,
        "equity": [equity[0] / MILLION, equity[1] / MILLION],
        "wa_shares": [shares[0] / MILLION],
    }
    return Parsed("CNY", values, period, quarterly=False)
