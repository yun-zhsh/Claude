"""从业绩公告原文中提取每股收益，并整理成逐季度和 TTM 数据。

提取依据的是业绩公告第一页的财务摘要表（已在腾讯 2014 年至今的公告上验证）：
  - 季度格式：EPS 一行有三个数，依次为本季度、去年同季度、上一季度；
  - 累计格式（2016 年中以前的中期、三季度、年度公告）：两个数，依次为
    本年初至今累计、去年同期累计。单季数据由累计数减去本年此前各季得到。
同一季度会在多份公告里出现（本期、下季度的环比、明年的同比），程序会
逐一比对，不一致时以最早公布的数值为准并给出提示。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.providers.hkex import Filing

MONTH_TO_QUARTER = {"MARCH": 1, "JUNE": 2, "SEPTEMBER": 3, "DECEMBER": 4}
PERIOD_RE = re.compile(r"ENDED (\d{1,2}) (MARCH|JUNE|SEPTEMBER|DECEMBER) (\d{4})", re.I)
EPS_RE = re.compile(
    r"(?P<non_ifrs>Non-IFRS |Non-GAAP )?EPS[\s()\d]*\((?P<ccy>[A-Z]{3}) per share\)[\s*]*\S?[–-] ?basic (?P<body>.*?)\S?[–-] ?diluted"
)
NUMBER_RE = re.compile(r"\(?\d+\.\d+\)?")
CURRENCY_CODES = {"RMB": "CNY"}  # 公告里的写法 -> ISO 货币代码
SERIES = ("eps", "non_ifrs_eps")
PARSER_VERSION = 1  # 解析规则变化时加 1，已保存的解析结果会自动重新解析
SERIES_LABELS = {"eps": "EPS", "non_ifrs_eps": "Non-IFRS EPS"}
TOLERANCE = 0.005  # 不同公告对同一季度的数值允许的差异（四舍五入、累计数相减）


@dataclass
class Announcement:
    period: pd.Period  # 本公告的报告季度，如 2026Q2
    published: pd.Timestamp  # 发布时间（香港时间）
    url: str
    currency: str
    is_quarterly_table: bool  # True：摘要表是单季数据；False：年初至今累计数据
    values: dict[str, list[float]]  # series -> 摘要表里该行的数值

    @property
    def available(self) -> pd.Timestamp:
        """第一个可以用到这份公告的收盘价所在日期。"""
        return available_date(self.published)


@dataclass
class Observation:
    value: float
    url: str
    derived: bool  # 是否由累计数相减得到


@dataclass
class QuarterValue:
    value: float
    available: pd.Timestamp
    observations: list[Observation] = field(default_factory=list)

    @property
    def max_deviation(self) -> float:
        return max(abs(o.value - self.value) for o in self.observations)


def parse_period(title: str) -> pd.Period:
    m = PERIOD_RE.search(title)
    if not m:
        raise ValueError(f"无法从标题识别报告期: {title}")
    return pd.Period(year=int(m.group(3)), quarter=MONTH_TO_QUARTER[m.group(2).upper()], freq="Q")


def available_date(published: pd.Timestamp) -> pd.Timestamp:
    """港股 16:00 收盘；收盘后发布的公告从下一个交易日起才反映在收盘价里。"""
    day = published.normalize()
    return day + pd.Timedelta(days=1) if published.hour >= 16 else day


def parse_eps_table(text: str) -> tuple[str, dict[str, list[float]]]:
    """从摘要表文字中找出 IFRS 和 Non-IFRS 基本 EPS 行，返回（币种，各行数值）。"""
    text = " ".join(text.split())
    values: dict[str, list[float]] = {}
    currency = None
    for m in EPS_RE.finditer(text):
        key = "non_ifrs_eps" if m.group("non_ifrs") else "eps"
        if key in values:
            continue
        nums = [t for t in m.group("body").split() if NUMBER_RE.fullmatch(t)]
        values[key] = [-float(t.strip("()")) if t.startswith("(") else float(t) for t in nums]
        currency = currency or CURRENCY_CODES.get(m.group("ccy"), m.group("ccy"))
    if "eps" not in values:
        raise ValueError("没有找到 EPS 行")
    return currency, values


def parse_announcement(filing: Filing) -> Announcement:
    """解析一份业绩公告，数值保持公告原样（未做拆股调整）。"""
    period = parse_period(filing.title)
    text = PdfReader(io.BytesIO(filing.pdf)).pages[0].extract_text() or ""
    currency, values = parse_eps_table(text)
    counts = {len(v) for v in values.values()}
    if counts == {3}:
        quarterly = True
    elif counts == {2}:
        quarterly = period.quarter == 1  # 一季度公告的“累计”就是单季
    else:
        raise ValueError(f"{filing.url}: EPS 行的数值个数不符合预期: {values}")
    return Announcement(period, filing.published, filing.url, currency, quarterly, values)


def split_factor(published: pd.Timestamp, splits: pd.Series) -> float:
    """公告发布之后发生的拆股，需要把公告里的每股数据除以的倍数。"""
    if splits.empty:
        return 1.0
    after = splits[splits.index > published.normalize()]
    return float(after.prod()) if len(after) else 1.0


def adjust_for_splits(announcements: list[Announcement], splits: pd.Series) -> list[Announcement]:
    """把每股数据换算到当前股本口径（与复权股价一致）。"""
    out = []
    for a in announcements:
        factor = split_factor(a.published, splits)
        out.append(replace(a, values={k: [v / factor for v in vs] for k, vs in a.values.items()}))
    return out


ANNOUNCEMENT_COLUMNS = ["published", "period", "table", "currency", "eps", "non_ifrs_eps", "url", "parser_version"]


def announcements_to_frame(announcements: list[Announcement]) -> pd.DataFrame:
    """解析结果的存储格式：每份公告一行，摘要表里的数值用分号连接（公告原样数值）。"""
    rows = [
        {
            "published": f"{a.published:%Y-%m-%d %H:%M}",
            "period": str(a.period),
            "table": "quarterly" if a.is_quarterly_table else "cumulative",
            "currency": a.currency,
            **{s: ";".join(f"{v:g}" for v in a.values[s]) if s in a.values else "" for s in SERIES},
            "url": a.url,
            "parser_version": PARSER_VERSION,
        }
        for a in sorted(announcements, key=lambda a: a.published)
    ]
    return pd.DataFrame(rows, columns=ANNOUNCEMENT_COLUMNS)


def announcements_from_frame(df: pd.DataFrame) -> list[Announcement]:
    out = []
    for r in df.fillna("").to_dict("records"):
        out.append(
            Announcement(
                period=pd.Period(r["period"], freq="Q"),
                published=pd.Timestamp(r["published"]),
                url=r["url"],
                currency=r["currency"],
                is_quarterly_table=r["table"] == "quarterly",
                values={s: [float(v) for v in str(r[s]).split(";")] for s in SERIES if str(r[s])},
            )
        )
    return out


@dataclass
class EarningsHistory:
    currency: str
    quarters: dict[str, dict[pd.Period, QuarterValue]]  # series -> 季度 -> 单季值
    ttm: pd.DataFrame  # report_date, available_date, eps_ttm, non_ifrs_eps_ttm
    announcements: list[Announcement]

    def discrepancies(self) -> list[str]:
        out = []
        for series, quarters in self.quarters.items():
            for p, q in sorted(quarters.items()):
                if q.max_deviation > TOLERANCE:
                    vals = ", ".join(f"{o.value:.3f}{'(推算)' if o.derived else ''}" for o in q.observations)
                    out.append(f"{SERIES_LABELS[series]} {p} 在不同公告中数值不一致（可能是公司重述）[{vals}]，采用最早公布的 {q.value:.3f}")
        return out

    def quarters_frame(self) -> pd.DataFrame:
        rows = []
        for p in sorted(set().union(*self.quarters.values())):
            row = {"quarter": str(p), "currency": self.currency}
            for series in SERIES:
                q = self.quarters[series].get(p)
                row[series] = round(q.value, 4) if q else None
                row[f"{series}_available"] = q.available.date() if q else None
                row[f"{series}_sources"] = len(q.observations) if q else 0
                row[f"{series}_max_deviation"] = round(q.max_deviation, 4) if q else None
            row["first_source"] = next(
                (q.observations[0].url for s in SERIES if (q := self.quarters[s].get(p))), None
            )
            rows.append(row)
        return pd.DataFrame(rows)


def build_history(parsed: list[Announcement]) -> EarningsHistory:
    announcements: dict[pd.Period, Announcement] = {}
    for a in sorted(parsed, key=lambda x: x.available):
        announcements.setdefault(a.period, a)  # 同一报告期的重复公告只取最早一份

    currencies = {a.currency for a in announcements.values()}
    if len(currencies) != 1:
        raise ValueError(f"公告中的 EPS 币种不一致: {currencies}")

    quarters: dict[str, dict[pd.Period, QuarterValue]] = {s: {} for s in SERIES}
    ytd: dict[str, dict[pd.Period, float]] = {s: {} for s in SERIES}
    ttm_rows = []

    def record(series: str, period: pd.Period, value: float, a: Announcement, derived: bool) -> None:
        q = quarters[series].setdefault(period, QuarterValue(value, a.available))
        q.observations.append(Observation(value, a.url, derived))

    for a in sorted(announcements.values(), key=lambda x: x.available):
        for series, vals in a.values.items():
            if a.is_quarterly_table:
                for period, value in zip([a.period, a.period - 4, a.period - 1], vals):
                    record(series, period, value, a, derived=False)
            else:
                for period, value in zip([a.period, a.period - 4], vals):
                    ytd[series].setdefault(period, value)
            _derive_from_ytd(series, quarters[series], ytd[series], a, record)

        last4 = [a.period - k for k in range(4)]
        row = {"report_date": a.period.end_time.normalize(), "available_date": a.available}
        for series in SERIES:
            qs = quarters[series]
            row[f"{series}_ttm"] = round(sum(qs[p].value for p in last4), 4) if all(p in qs for p in last4) else None
        ttm_rows.append(row)

    ttm = pd.DataFrame(ttm_rows, columns=["report_date", "available_date", "eps_ttm", "non_ifrs_eps_ttm"])
    return EarningsHistory(currencies.pop(), quarters, ttm, list(announcements.values()))


def _derive_from_ytd(series, quarters, ytd, a, record) -> None:
    """用年初至今累计数推算尚未知道的单季数：Qn = 累计(n) - 累计(n-1) 或 - 本年此前各季之和。"""
    changed = True
    while changed:
        changed = False
        for p, total in ytd.items():
            if p in quarters:
                continue
            earlier = [p - k for k in range(1, p.quarter)]
            if p.quarter > 1 and (p - 1) in ytd:
                value = total - ytd[p - 1]
            elif all(e in quarters for e in earlier):
                value = total - sum(quarters[e].value for e in earlier)
            else:
                continue
            record(series, p, value, a, derived=True)
            changed = True
