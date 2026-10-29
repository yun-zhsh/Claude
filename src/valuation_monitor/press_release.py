"""从美股公司的业绩新闻稿（HTML）里提取业绩数据（京东已在 2014 年 8 月至今的 49 份新闻稿上验证）。

新闻稿里的表格是“表头 + 数据行”的版式：
  表头第一行是各组的说明（For the three months ended / six months ended / year ended），
  接着是日期行和币种行（RMB、RMB、US$），每组以一个美元列结束。
  合并损益表里有季度一组（和年初至今一组），需要的是“三个月”那一组，各列用日期区分：
  本季度、去年同季度，有的年份还有上一季度。
  营收取 Total net revenues，净利润取归属于普通股股东的那一行，EPS 取“每份 ADS”的基本 EPS（人民币），
  Non-GAAP EPS 取“Non-GAAP 每份 ADS”的基本 EPS；资产负债表里取归属公司股东的权益（本期末、上年末）。
金额单位（千元或百万元）写在表格前面的说明里，统一换算成百万元。
同一数值在不同表格里重复出现时必须一致，否则报错，不会悄悄取其中一个。
"""

from __future__ import annotations

import re
from typing import NamedTuple

import pandas as pd

DATE_RE = re.compile(r"^([A-Z][a-z]+) ?(\d{1,2}), (\d{4})$")  # 个别新闻稿里有“September30”这样漏掉空格的写法
CURRENCY_CELL_RE = re.compile(r"^(RMB|US\$|USD|HK\$)$")
NUMBER_CELL_RE = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?$")
UNIT_RE = re.compile(r"in (thousands|millions)", re.I)
GROUP_RE = re.compile(r"(three|six|nine|twelve) months ended|year ended|as of", re.I)
# 归属普通股股东的净利润合计。有终止经营业务的年份还有“持续经营”“终止经营”的分项，不能取
PROFIT_LABEL_RE = re.compile(r"^net (?!.*(?:continuing|discontinued|non-controlling|non-gaap)).*attributable to", re.I)
EQUITY_LABEL_RE = re.compile(r"^total jd\.com,? inc\.? shareholders.? equity", re.I)
TOTAL_EQUITY_LABEL_RE = re.compile(r"^total shareholders?'?s? equity$", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
AMOUNT_KEYS = ("revenue", "profit")
TOLERANCE = 0.001  # 同一数值在不同表格里的允许差异（相对）


class Parsed(NamedTuple):
    currency: str
    values: dict[str, list[float]]  # series -> 本期、去年同期、（上一期）；equity 为本期末、上年末
    period: pd.Period  # 报告季度
    quarterly: bool = True


def clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").replace("’", "'").split())


def to_number(cell: str) -> float | None:
    cell = cell.replace("—", "0") if cell == "—" else cell
    if not NUMBER_CELL_RE.match(cell):
        return None
    value = float(cell.strip("()").replace(",", ""))
    return -value if cell.startswith("(") else value


def table_rows(table) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        rows.append([c for c in cells if c and c not in (")", "%", ")%", "$")])
    return [r for r in rows if r]


def parse_date(cell: str) -> pd.Timestamp | None:
    m = DATE_RE.match(cell)
    if not m or m.group(1) not in MONTHS:
        return None
    return pd.Timestamp(year=int(m.group(3)), month=MONTHS[m.group(1)], day=int(m.group(2)))


class Header(NamedTuple):
    dates: list[pd.Timestamp]
    currencies: list[str]
    groups: list[list[int]]  # 每一组包含的列号
    labels: list[str]  # 每一组的说明（可能为空）
    first_data_row: int
    inherited: bool = False  # 沿用前一张表的表头


def parse_header(rows: list[list[str]]) -> Header | None:
    for i, row in enumerate(rows[:8]):
        dates = [parse_date(c) for c in row]
        if len(row) >= 2 and all(d is not None for d in dates):
            break
    else:
        return None
    if i + 1 >= len(rows) or not all(CURRENCY_CELL_RE.match(c) for c in rows[i + 1]) or len(rows[i + 1]) != len(dates):
        return None
    currencies = rows[i + 1]
    groups, current = [], []
    for j, ccy in enumerate(currencies):
        current.append(j)
        if ccy != "RMB":  # 每组以美元列结束
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    labels = [c for r in rows[:i] for c in r if GROUP_RE.search(c)]
    return Header(dates, currencies, groups, labels if len(labels) == len(groups) else [""] * len(groups), i + 2)


def unit_scale(table, rows: list[list[str]]) -> float:
    """金额换算成百万元的系数。单位写在表内的行里，或表格前面的说明里。"""
    for r in rows[:6]:
        if len(set(r)) == 1 and (m := UNIT_RE.search(r[0])):  # 有的单位行把同一个说明重复了几格
            return 0.001 if m.group(1).lower() == "thousands" else 1.0
    for s in table.find_all_previous(string=True, limit=40):
        if m := UNIT_RE.search(clean(s)):
            return 0.001 if m.group(1).lower() == "thousands" else 1.0
    raise ValueError("找不到金额单位")


def columns_by_date(header: Header, group: int, values: list[float]) -> dict[pd.Timestamp, float]:
    """一行数值（只含数字，不含标签）里，人民币列对应的数值，按日期索引。"""
    extra = 0 if header.inherited else 1  # 有自己表头的表里，多出的一个数是同比百分比
    if not len(header.dates) <= len(values) <= len(header.dates) + extra:
        return {}
    out = {}
    for col in header.groups[group]:
        if header.currencies[col] == "RMB":
            out[header.dates[col]] = values[col]
    return out


def numbers(row: list[str]) -> list[float]:
    return [v for c in row[1:] if (v := to_number(c)) is not None]


def quarter_offsets(by_date: dict[pd.Timestamp, float]) -> tuple[pd.Period, list[float]]:
    """按日期整理成 [本季度, 去年同季度, （上一季度）]。"""
    periods = {pd.Period(d, freq="Q"): v for d, v in by_date.items()}
    current = max(periods)
    out = [periods.get(current), periods.get(current - 4), periods.get(current - 1)]
    if out[0] is None or out[1] is None:
        raise ValueError(f"季度数据缺少本期或去年同期: {sorted(periods)}")
    return current, out[:2] if out[2] is None else out


def parse_flow_rows(rows, header: Header, scale: float | None) -> dict[str, tuple[pd.Period, list[float]]]:
    """“三个月”那一组里的营收、净利润、每份 ADS 的基本 EPS 和 Non-GAAP 基本 EPS。

    合并损益表可能被分页切成几张表，所以每张表只取自己有的行，由调用方合并并核对。
    """
    group = next((g for g, label in enumerate(header.labels) if re.search("three months", label, re.I)), None)
    if group is None:
        return {}
    found: dict[str, dict[pd.Timestamp, float]] = {}
    section = None  # 当前在哪个“每份 ADS”小节：eps 或 non_ifrs_eps
    basic = False  # 小节里是否在“Basic”分段下（有终止经营业务的年份，Basic 下面再分持续经营、终止经营和合计）
    for row in rows[header.first_data_row :]:
        label = row[0]
        nums = numbers(row)
        low = label.lower()
        if not nums:
            if "per ads" in low:
                section = "non_ifrs_eps" if re.search("non-gaap|adjusted", low) else "eps" if low.startswith("net ") else None
                basic = False
            elif low == "basic":
                basic = True
            elif low == "diluted":
                basic = False
            else:
                section = None
            continue
        if section and section not in found and (low == "basic" or (basic and re.match(r"net (income|loss).*per ads", low))):
            found[section] = columns_by_date(header, group, nums)
        elif re.match(r"total net revenues", low) and "revenue" not in found:
            found["revenue"] = columns_by_date(header, group, nums)
        elif PROFIT_LABEL_RE.match(label) and "profit" not in found:
            found["profit"] = columns_by_date(header, group, nums)
    out = {}
    for key, by_date in found.items():
        if not by_date:
            continue
        period, vals = quarter_offsets(by_date)
        if key in AMOUNT_KEYS:
            if scale is None:
                continue  # 单位不明的表格不用，其他表格里还有同样的数
            vals = [v * scale for v in vals]
        out[key] = (period, vals)
    return out


def parse_balance_table(rows, header: Header, scale: float | None) -> tuple[pd.Period, list[float]] | None:
    """资产负债表：归属公司股东的权益，依次为本期末、上年末。"""
    labels = [r[0] for r in rows]
    if not any(re.match(r"total (liabilities|assets)", label, re.I) for label in labels):
        return None
    if scale is None:
        return None
    has_nci = any(re.match(r"non-controlling interests?$", label, re.I) for label in labels)
    pattern = EQUITY_LABEL_RE if has_nci else re.compile(f"{EQUITY_LABEL_RE.pattern}|{TOTAL_EQUITY_LABEL_RE.pattern}", re.I)
    for row in rows[header.first_data_row :]:
        if pattern.match(row[0]):
            by_date = columns_by_date(header, 0, numbers(row))
            if len(by_date) < 2:
                return None
            current = max(by_date)
            earlier = [d for d in by_date if d != current]
            return pd.Period(current, freq="Q"), [by_date[current] * scale, by_date[max(earlier)] * scale]
    return None


def close(a: float, b: float, key: str) -> bool:
    return abs(a - b) <= (0.005 if key in ("eps", "non_ifrs_eps") else max(1.0, abs(a) * TOLERANCE))


def parse_press_release(html: bytes) -> Parsed:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    candidates: dict[str, list[tuple[pd.Period, list[float]]]] = {}
    previous = None  # 紧挨着的前一张表的表头（前一张表没有自己的表头时为 None）
    for table in soup.find_all("table"):
        rows = table_rows(table)
        header = parse_header(rows)
        own, header = header, header or (previous and previous._replace(first_data_row=0, inherited=True))
        previous = own
        if header is None:
            continue
        try:
            scale = unit_scale(table, rows)
        except ValueError:
            scale = None
        for key, found in parse_flow_rows(rows, header, scale).items():
            if header.inherited and key not in ("eps", "non_ifrs_eps"):
                continue  # 损益表被分页切开时，后半张表（每股数据）没有表头，沿用前一张的；金额只认有自己表头的表
            candidates.setdefault(key, []).append(found)
        if not header.inherited and (equity := parse_balance_table(rows, header, scale)) is not None:
            candidates.setdefault("equity", []).append(equity)

    missing = {"revenue", "profit", "eps", "equity"} - candidates.keys()
    if missing:
        raise ValueError(f"没有找到: {sorted(missing)}")
    values: dict[str, list[float]] = {}
    periods = set()
    for key, found in candidates.items():
        (period, first), *others = found
        for p, other in others:
            if p != period or any(not close(a, b, key) for a, b in zip(other, first)):
                raise ValueError(f"{key} 在不同表格中不一致: {found}")
        values[key] = first
        periods.add(period)
    if len(periods) != 1:
        raise ValueError(f"各行的报告期不一致: {periods}")
    return Parsed("CNY", values, periods.pop())
