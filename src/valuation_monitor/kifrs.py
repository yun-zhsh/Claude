"""从三星电子英文版合并财务报表（K-IFRS）中提取业绩数据。

每个季度读两份文件：合并损益表（soi）和合并财务状况表（bs）。
  - 损益表的列：本期、去年同期的韩元（KRW）金额，再是同样两列的美元换算，然后是年初至今的各列。
    一至三季度取“三个月”的前两列（本季度、去年同季度）；第四季度报告期是全年，取前两列
    （本年、上年），当作累计数，单季数由程序用全年减前三季度得到。
  - 营收取 Revenue，净利润取 Profit attributable to owners of the parent，EPS 取基本 EPS（普通股，韩元）；
    金额单位是百万韩元。
  - 归属母公司股东的净资产 = Total equity − Non-controlling interests，依次为本期末、上年末。
三星有普通股和优先股，净利润包含归属优先股的部分，所以“净利润 ÷ 基本 EPS”反推的是普通股加优先股的合计加权股数。
"""

from __future__ import annotations

import io
import re

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.parsed import Parsed

AMT = r"\(?\d{1,3}(?:,\d{3})+\)?"  # 带千位分隔的金额；附注编号（如 27、18, 19）不算
EPS_AMT = r"\(?\d[\d,]*\)?"
TITLE_RE = re.compile(r"(?P<q>[1-4])Q (?P<year>20\d\d)")
PERIOD_END = {1: "March 31", 2: "June 30", 3: "September 30", 4: "December 31"}
REVENUE_RE = re.compile(rf"\bRevenue(?: [\d, ]{{1,8}}?)? (?P<a>{AMT}) (?P<b>{AMT})")
PROFIT_RE = re.compile(rf"Profit attributable to:? (?:owners of the (?:parent(?: company)?|Company)\s+)?(?P<a>{AMT}) (?P<b>{AMT})", re.I)
EPS_RE = re.compile(rf"Earnings per share.{{0,200}}?- Basic (?P<a>{EPS_AMT}) (?P<b>{EPS_AMT})", re.I)
TOTAL_EQUITY_RE = re.compile(rf"Total equity (?P<a>{AMT}) (?P<b>{AMT})")
NCI_RE = re.compile(rf"Non-controlling interests(?: \d{{1,2}})? (?P<a>{AMT}) (?P<b>{AMT})", re.I)


def read_text(pdf: bytes) -> str:
    text = " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)
    text = " ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).split())  # 个别文件里夹着控制字符
    return re.sub(r"(\d), (\d{3})\b", r"\1,\2", text)  # PDF 文字提取会在千位分隔符后插入空格，如“44, 260,956”


def to_number(token: str) -> float:
    value = float(token.strip("()").replace(",", ""))
    return -value if token.startswith("(") else value


def pair(regex: re.Pattern, text: str, name: str) -> list[float]:
    m = regex.search(text)
    if not m:
        raise ValueError(f"没有找到 {name}")
    return [to_number(m.group("a")), to_number(m.group("b"))]


def parse_kifrs(title: str, soi: bytes, bs: bytes) -> Parsed:
    m = TITLE_RE.search(title)
    if not m:
        raise ValueError(f"无法从标题识别报告期: {title}")
    quarter, year = int(m.group("q")), int(m.group("year"))
    income, balance = read_text(soi), read_text(bs)
    if f"{PERIOD_END[quarter]}, " not in income and f"{PERIOD_END[quarter]}," not in income.replace(" ,", ","):
        raise ValueError(f"损益表里没有 {PERIOD_END[quarter]}，与标题的报告期 {quarter}Q {year} 不符")

    total, nci = pair(TOTAL_EQUITY_RE, balance, "Total equity"), pair(NCI_RE, balance, "Non-controlling interests")
    values = {
        "revenue": pair(REVENUE_RE, income, "Revenue"),
        "profit": pair(PROFIT_RE, income, "Profit attributable to owners of the parent"),
        "eps": pair(EPS_RE, income, "Basic EPS"),
        "equity": [t - n for t, n in zip(total, nci)],
    }
    return Parsed("KRW", values, pd.Period(year=year, quarter=quarter, freq="Q"), quarterly=quarter != 4)
