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
import sys

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


def read_text_of_page(page) -> str:
    return clean_text(page.extract_text() or "")


def read_text(pdf: bytes) -> str:
    return clean_text(" ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages))


def clean_text(text: str) -> str:
    text = " ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).split())  # 个别文件里夹着控制字符
    text = re.sub(r"(\d), (\d{3})\b", r"\1,\2", text)  # PDF 文字提取会在千位分隔符后插入空格，如“44, 260,956”
    # 也会把最后一组数字拆开，如“5,969,78 3”：前面不足三位、加上后面刚好三位的才合并
    return re.sub(r"(?<![\d,])(\d{1,3}(?:,\d{3})*,)(\d{1,2}) (\d{1,2})(?![\d,])", lambda m: m.group(1) + m.group(2) + m.group(3) if len(m.group(2) + m.group(3)) == 3 else m.group(0), text)


def to_number(token: str) -> float:
    value = float(token.strip("()").replace(",", ""))
    return -value if token.startswith("(") else value


def pair(regex: re.Pattern, text: str, name: str) -> list[float]:
    m = regex.search(text)
    if not m:
        raise ValueError(f"没有找到 {name}")
    return [to_number(m.group("a")), to_number(m.group("b"))]


# EPS 附注（Notes 里的 Earnings per Share）：加权平均股数，单位千股。
# 第一、二、三季度的附注里每年两列（三个月、累计），共四个数：本年三个月、本年累计、去年三个月、去年累计；
# 第一季度和全年只有本年、去年两个数。
SHARE_ROW = r"Weighted-average number of {kind} shares outstanding(?: \(in thousands\))? (?P<n>(?:[\d,]+ ){{1,4}})"
COMMON_SHARES_RE = re.compile(SHARE_ROW.format(kind="(?:ordinary|common)"))
PREFERRED_SHARES_RE = re.compile(SHARE_ROW.format(kind="(?:preferred|preference)"))


def eps_note_pages(note: bytes) -> bytes | None:
    """从完整的附注 PDF 里只留下 Earnings per Share 附注所在的页（原文页面原样保留；优先股部分常在下一页），
    下载保存时用，省得每个季度存几百 KB。"""
    from pypdf import PdfWriter

    reader = PdfReader(io.BytesIO(note))
    texts = [read_text_of_page(page) for page in reader.pages]
    # 要的是附注里的表（“Weighted-average number of ordinary shares outstanding 5,767,030 …”），不是会计政策里的文字说明
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
    """附注里披露的普通股加优先股基本 EPS 加权平均股数（附注原样的数，单位不定），依次为本期、去年同期。

    单位在不同年份的附注里不一样（千股、股），由调用方对照净利润 ÷ EPS 的量级来判断。
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
    """附注里的股数是千股还是股？按哪个单位换算后与“归母净利润 ÷ 基本 EPS”相差不到 5% 就用哪个。"""
    for factor in (1e-6, 1e-3, 1.0):
        if abs(raw[0] * factor / implied_millions - 1) < 0.05:
            return [v * factor for v in raw]
    return None


def parse_kifrs(title: str, soi: bytes, bs: bytes, note: bytes | None = None) -> Parsed:
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
    if note is not None and (raw := parse_weighted_shares(note)):
        scaled = scale_to_millions(raw, values["profit"][0] / values["eps"][0])
        if scaled is None:
            print(f"{title}: 附注里的股数 {raw[0]:,.0f} 与净利润 ÷ EPS 的量级对不上，无法判断单位，股数改为反推", file=sys.stderr)
        else:
            values["wa_shares"] = scaled
    return Parsed("KRW", values, pd.Period(year=year, quarter=quarter, freq="Q"), quarterly=quarter != 4)


# 业绩发布会（Earnings Call）材料，2026 年起的版式：三列依次为去年同季度、上一季度、本季度
DECK_PCT_COLUMNS = r"(?P<a>[\d.]+) [\d.]+% (?P<b>[\d.]+) [\d.]+% (?P<c>[\d.]+) [\d.]+%"
DECK_SALES_RE = re.compile(rf"\bSales {DECK_PCT_COLUMNS}")
DECK_PROFIT_RE = re.compile(rf"Profit ?attributable to owners of the parent {DECK_PCT_COLUMNS}")
DECK_EPS_RE = re.compile(r"EPS ?\(KRW\) ?※ ?Common stock (?P<a>[\d,]+) (?P<b>[\d,]+) (?P<c>[\d,]+)")
DECK_EQUITY_RE = re.compile(r"Shareholder equity (?P<a>[\d,]+\.\d) (?P<b>[\d,]+\.\d) (?P<c>[\d,]+\.\d)")
# 第四季度的材料多了全年两列：去年同季度、上一季度、本季度、上年全年、本年全年（只有营收、净利润带“占比%”）
FY_PCT = r"(?P<q4y>[\d.]+) (?P<q3>[\d.]+) [\d.]+% (?P<q4>[\d.]+) [\d.]+% (?P<fy0>[\d.]+) [\d.]+% (?P<fy1>[\d.]+) [\d.]+%"
DECK_FY_SALES_RE = re.compile(rf"\bSales {FY_PCT}")
DECK_FY_PROFIT_RE = re.compile(rf"Profit ?attributable to owners of the parent {FY_PCT}")
DECK_FY_EPS_RE = re.compile(
    r"(?:EPS ?\(KRW\) ?※ ?Common stock|Earnings per share ?\(KRW\)) (?P<q4y>[\d,]+) (?P<q3>[\d,]+) (?P<q4>[\d,]+) (?P<fy0>[\d,]+) (?P<fy1>[\d,]+)"
)
TRILLION = 1_000_000  # 百万韩元
BILLION = 1_000


def deck_columns(regex: re.Pattern, text: str, name: str) -> list[float]:
    """发布会材料里的一行，返回 [本季度, 去年同季度, 上一季度]。"""
    m = regex.search(text)
    if not m:
        raise ValueError(f"发布会材料里没有找到 {name}（版式可能变了）")
    yoy, qoq, cur = (float(m.group(k).replace(",", "")) for k in "abc")
    return [cur, yoy, qoq]


def fiscal_year_columns(regex: re.Pattern, text: str, name: str) -> list[float]:
    """第四季度材料里的一行，返回 [本年全年, 上年全年]。"""
    m = regex.search(text)
    if not m:
        raise ValueError(f"第四季度发布会材料里没有找到 {name}（版式可能变了）")
    return [float(m.group("fy1").replace(",", "")), float(m.group("fy0").replace(",", ""))]


def parse_kifrs_deck(title: str, deck: bytes, previous_bs: bytes) -> Parsed:
    """业绩发布会材料里的当季业绩（审阅前，先用着，英文财报发布后换成财报数字）。

    营收和净利润只精确到 0.1 万亿韩元；EPS 是精确的韩元数。材料里的净资产含非控股权益，
    归属母公司的净资产按上一季度财报里“归母 ÷ 合计”的比例估算（非控股权益只占 2%～3%，变化很小）。
    第四季度和年度财报一样当作全年累计数（本年、上年）处理，由程序用全年减前三季度得到单季数，
    这样第四季度的 TTM EPS 与年报的全年 EPS 一致，财报发布后数字也不会跳变。
    """
    m = TITLE_RE.search(title)
    if not m:
        raise ValueError(f"无法从标题识别报告期: {title}")
    quarter, year = int(m.group("q")), int(m.group("year"))
    text = read_text(deck)
    if f"{quarter}Q {year} Results" not in text:
        raise ValueError(f"发布会材料不是 {quarter}Q {year} 的，或版式已变")

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
