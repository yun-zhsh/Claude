"""从业绩公告原文中提取业绩数据，并整理成逐期间（季度或半年）和 TTM 数据。

提取的数据：营收、归属股东净利润（百万元）、基本 EPS（元/股）、Non-IFRS 基本 EPS（有的公司没有）、
归属股东净资产（百万元）。各公司公告的版式不同，用 FORMATS 里的解析函数分别处理：

  highlights（腾讯，已在 2014 年至今的公告上验证）
    - 第一页财务摘要表：营收、归属股东净利润、基本 EPS、Non-IFRS 基本 EPS。
        季度格式：每行三个数，依次为本季度、去年同季度、上一季度；
        累计格式（2016 年中以前的中期、三季度、年度公告）：两个数，依次为本年初至今累计、
        去年同期累计。
    - 资产负债表：归属股东净资产，依次为本期末、上年末。
  statements（舜宇光学，已在 2014 年至今的中期和年度公告上验证）
    - 合并损益表：营收、归属股东净利润、基本 EPS（单位为“人民币分”），都是两个数，依次为
      本期累计、去年同期累计；金额单位是千元，读取时统一换算成百万元。
    - 资产负债表：同上。

公司可以每季度发布业绩（4 期/年），也可以只发中期和年度（2 期/年，下半年的数据由全年减上半年得到）。
期间数由已有的公告推断，TTM 是最近一年内各期之和（4 个季度或 2 个半年）。

同一数值会在多份公告里出现（本期、下期的环比、明年的同比、资产负债表的上年末），
程序会逐一比对，不一致时以最早公布的数值为准并给出提示。

股数没有直接提取，而是用“当期归属股东净利润 ÷ 当期基本 EPS”反推，即当期加权平均股数。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.market_calendar import HK, Market, effective_trading_day, get_market
from valuation_monitor.audit import Component, num, shares_formula, split_notes, sum_formula
from valuation_monitor.kifrs import parse_kifrs, parse_kifrs_deck
from valuation_monitor.press_release import parse_press_release
from valuation_monitor.providers.base import Filing

MONTH_TO_QUARTER = {"MARCH": 1, "JUNE": 2, "SEPTEMBER": 3, "DECEMBER": 4}
PERIOD_RE = re.compile(r"ENDED (\d{1,2}) (MARCH|JUNE|SEPTEMBER|DECEMBER) (\d{4})", re.I)
EPS_RE = re.compile(
    r"(?P<non_ifrs>Non-IFRS |Non-GAAP )?EPS[\s()\d]*\((?P<ccy>[A-Z]{3}) per share\)[\s*]*\S?[–-] ?basic (?P<body>.*?)\S?[–-] ?diluted"
)
NUMBER_RE = re.compile(r"\(?\d+\.\d+\)?")
# 摘要表里金额行的标签：后面紧跟（可能带脚注标记的）数字才算，避免匹配到正文里的同名词语
REVENUE_RE = re.compile(r"\bRevenues? (?=[(*\d])")
PROFIT_RE = re.compile(r"(?<!IFRS )(?<!GAAP )Profit attributable to (?:the Company.s )?equity holders of the Company (?=[(*\d])")
EQUITY_RE = re.compile(
    r"EQUITY Equity attributable to (?:the Company.s )?equity holders(?: of the Company)?(?P<body>.*?)Non-controlling interests"
)
AMOUNT_RE = re.compile(r"\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?")
FOOTNOTE_RE = re.compile(r"\(\d\)|\*+")
CURRENCY_CODES = {"RMB": "CNY", "HK$": "HKD", "HKD": "HKD", "US$": "USD", "USD": "USD"}  # 公告里的写法 -> ISO 货币代码

# 腾讯：EPS 附注里的加权平均股数行（百万股），三个月和累计各两个数：本期、去年同期
WA_SHARES_RE = re.compile(
    r"Weighted average number of ordinary shares in issue(?: excluding [^()]*?)?(?:\s*\([^)]*\))*\s*((?:[\d,]+(?:\.\d+)?\s+){1,4})(?=[A-Z])", re.I
)
# 舜宇：EPS 附注里的加权平均股数行（千股），本期累计、去年同期累计
WA_SHARES_STMT_RE = re.compile(
    r"Weighted average number of ordinary shares for the purpose of basic earnings per share(?: \(Note\))? (?P<a>[\d,]+) (?P<b>[\d,]+)", re.I
)

# statements 版式：合并损益表和资产负债表里的行
AMT = r"\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?(?![\d,])"  # 带千位分隔的金额；“3,”这样的附注编号不算
NOTE = r"\d{1,2}[A-Za-z]?(?:, ?\d{1,2}[A-Za-z]?)*"  # 附注编号：3、3A、3, 4、3,4
OWNERS = r"(?:owners|equity holders|shareholders) of the Company"
REVENUE_STMT_RE = re.compile(rf"\bRevenue (?:{NOTE} )?(?P<a>{AMT}) (?P<b>{AMT})")
PROFIT_STMT_RE = re.compile(rf"attributable to:? {OWNERS} (?P<a>{AMT}) (?P<b>{AMT})", re.I)
EQUITY_STMT_RE = re.compile(rf"Equity attributable to {OWNERS} (?P<a>{AMT}) (?P<b>{AMT})", re.I)
EPS_STMT_RE = re.compile(
    rf"Earnings per share\s*[–-]\s*Basic \((?P<ccy>RMB|HK\$|HKD|US\$|USD)(?P<cents> cents)?\) (?:{NOTE} )?(?P<a>\(?-?[\d.]+\)?) (?P<b>\(?-?[\d.]+\)?)",
    re.I,
)
# 净利润行前面的标签。综合收益总额那一行的写法和净利润行完全一样，只能靠标签区分
PROFIT_LABEL_RE = re.compile(r"\bprofit(?: \(loss\))? for the (?:period|year)\s*$", re.I)
UNIT_RE = re.compile(r"RMB\W{0,2}(?P<unit>000|million)", re.I)  # 表头里的金额单位：RMB’000 或 RMB million

PER_SHARE = ("eps", "non_ifrs_eps")  # 每股数据（元/股），拆股时需要调整
AMOUNTS = ("revenue", "profit")  # 金额（百万元）
FLOWS = PER_SHARE + AMOUNTS  # 期间数据：可以按季度拆分、加总成 TTM
BALANCES = ("equity",)  # 时点数据（百万元）
SHARE_COUNTS = ("wa_shares",)  # 公告披露的基本 EPS 所用的加权平均股数（百万股；ADR 为百万份 ADS），拆股时要乘以拆股倍数
SERIES = FLOWS + BALANCES + SHARE_COUNTS
SERIES_LABELS = {
    "eps": "EPS",
    "non_ifrs_eps": "Non-IFRS EPS",
    "revenue": "营收",
    "profit": "归母净利润",
    "equity": "归母净资产",
    "wa_shares": "加权平均股数",
}
PARSER_VERSIONS = {"highlights": 4, "statements": 2, "press_release": 2, "kifrs_statements": 3}  # 解析规则变化时加 1，已保存的解析结果会自动重新解析
PER_SHARE_TOLERANCE = 0.005  # 不同公告对同一数值允许的差异：每股数据（四舍五入、累计数相减）
PER_SHARE_RELATIVE_TOLERANCE = 5e-4
AMOUNT_TOLERANCE = 0.001  # 金额按相对差异
SHARES_CHANGE_LIMIT = 0.05  # 相邻季度反推股数变化超过 5% 时提示（可能提取错误）

# 下载的 TTM 文件里的列顺序：先是公告信息，再是各期原始数据 → TTM 合计 → 公式 → 每股数据
TTM_FRAME_ORDER = [
    "report_date", "published", "available_date", "effective_date", "provisional",
    *[f"period_{k}" for k in range(1, 5)],
    *[f"eps_{k}" for k in range(1, 5)], "eps_ttm", "eps_ttm_formula",
    *[f"non_ifrs_eps_{k}" for k in range(1, 5)], "non_ifrs_eps_ttm", "non_ifrs_eps_ttm_formula",
    *[f"revenue_{k}" for k in range(1, 5)], "revenue_ttm", "revenue_ttm_formula",
    "profit_latest", "eps_latest", "shares", "shares_source", "shares_implied", "shares_formula",
    "revenue_per_share_ttm", "revenue_per_share_formula",
    "equity", "book_value_per_share", "book_value_per_share_formula", "split_note",
]
TTM_COLUMNS = [
    "report_date", "published", "available_date",
    "eps_ttm", "non_ifrs_eps_ttm", "revenue_ttm", "shares",
    "revenue_per_share_ttm", "equity", "book_value_per_share",
]


def tolerance(series: str, value: float) -> float:
    if series in PER_SHARE:  # 每股数据四舍五入到分或到元，如三星的 EPS 是整数韩元，拆股调整后会有零点几元的差
        return max(PER_SHARE_TOLERANCE, abs(value) * PER_SHARE_RELATIVE_TOLERANCE)
    return max(1.0, abs(value) * AMOUNT_TOLERANCE)


@dataclass
class Announcement:
    period: pd.Period  # 本公告的报告季度，如 2026Q2
    published: pd.Timestamp  # 发布时间（香港时间）
    url: str
    currency: str
    is_quarterly_table: bool  # True：摘要表是单季数据；False：年初至今累计数据
    values: dict[str, list[float]]  # series -> 该行的数值（equity 为本期末、上年末）
    market: str = "HK"  # 上市地，决定收盘时间
    provisional: bool = False  # 暂用的初步数据，正式财报发布后会被取代
    shares_basis: str = ""  # 公告披露的加权平均股数对应的期间：空表示就是本期，“全年”表示披露的是全年数（第四季度）

    @property
    def available(self) -> pd.Timestamp:
        """第一个可以用到这份公告的收盘价所在日期。"""
        return get_market(self.market).available_date(self.published)


@dataclass
class Observation:
    value: float
    url: str
    derived: bool  # 是否由累计数相减得到


@dataclass
class QuarterValue:
    value: float
    available: pd.Timestamp
    published: pd.Timestamp  # 最早公布这个数值的公告的发布时间
    observations: list[Observation] = field(default_factory=list)

    @property
    def max_deviation(self) -> float:
        return max(abs(o.value - self.value) for o in self.observations)


def parse_period(title: str) -> pd.Period:
    m = PERIOD_RE.search(title)
    if not m:
        raise ValueError(f"无法从标题识别报告期: {title}")
    return pd.Period(year=int(m.group(3)), quarter=MONTH_TO_QUARTER[m.group(2).upper()], freq="Q")


def period_label(p: pd.Period, periods_per_year: int) -> str:
    """期间的显示名：每季度发布的叫 2025Q4，每半年发布的叫 2025H2。"""
    if periods_per_year == 4:
        return str(p)
    if periods_per_year == 2:
        return f"{p.year}H{p.quarter // 2}"
    return f"{p.year}FY"


def to_number(token: str) -> float:
    value = float(token.strip("()").replace(",", ""))
    return -value if token.startswith("(") else value


def amounts_after(text: str, start: int, count: int) -> list[float]:
    """从 start 开始依次取 count 个金额（跳过百分比、脚注标记等）。"""
    out = []
    for token in text[start:].split():
        if AMOUNT_RE.fullmatch(token) and not FOOTNOTE_RE.fullmatch(token):
            out.append(to_number(token))
            if len(out) == count:
                break
    return out


class Pages:
    """按需提取 PDF 各页的文字（空白合并成单个空格），没用到的页面不做提取。"""

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
    """从第一页摘要表中提取 EPS、Non-IFRS EPS、营收、归属股东净利润，返回（币种，各行数值）。"""
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
        raise ValueError("没有找到 EPS 行")

    count = len(values["eps"])
    for key, pattern in (("revenue", REVENUE_RE), ("profit", PROFIT_RE)):
        m = pattern.search(text)
        if m:
            values[key] = amounts_after(text, m.end(), count)
    return currency, values


def parse_equity(text: str) -> list[float] | None:
    """从资产负债表中提取归属股东净资产（本期末、上年末）。"""
    m = EQUITY_RE.search(" ".join(text.split()))
    if not m:
        return None
    tokens = [t for t in m.group("body").split() if AMOUNT_RE.fullmatch(t)]
    return [to_number(t) for t in tokens[-2:]] if len(tokens) >= 2 else None


def parse_highlights(pages: Pages) -> tuple[str, dict[str, list[float]]]:
    """第一页摘要表 + 资产负债表（腾讯）。"""
    currency, values = parse_summary_table(pages[0])
    for i in range(1, len(pages)):
        equity = parse_equity(pages[i])
        if equity:
            values["equity"] = equity
            break
    for i in range(len(pages)):
        if m := WA_SHARES_RE.search(" ".join(pages[i].split()) + " "):
            values["wa_shares"] = [to_number(t) for t in m.group(1).split()]  # 先保留全部数值，由 parse_announcement 挑出对应期间的两个
            break
    return currency, values


def first_profit_match(text: str) -> re.Match | None:
    """归属股东净利润行：只认前面紧挨着“Profit for the period/year”标签的那一处。"""
    for m in PROFIT_STMT_RE.finditer(text):
        if PROFIT_LABEL_RE.search(text[max(0, m.start() - 45) : m.start()]):
            return m
    return None


def parse_statements(pages: Pages) -> tuple[str, dict[str, list[float]]]:
    """合并损益表和资产负债表（舜宇光学）。金额换算成百万元，EPS 换算成元。

    损益表里，归属股东净利润的旁边有同样写法的“综合收益总额”，资产负债表里的“归属股东净资产”
    也以同样的字样结尾。所以净利润要同时满足：在损益表区域内（营收行之后、EPS 行之前），
    并且前面的标签是“Profit for the period/year”。找不到就报错，不会误取到别的行。
    """
    start = next((i for i in range(len(pages)) if REVENUE_STMT_RE.search(" ".join(pages[i].split()))), None)
    if start is None:
        raise ValueError("没有找到合并损益表")

    values: dict[str, list[float]] = {}
    currency = None
    scale = None  # 金额换算成百万元的系数

    def amounts(m: re.Match) -> list[float]:
        if scale is None:
            raise ValueError("找不到金额单位")
        return [to_number(m.group("a")) * scale, to_number(m.group("b")) * scale]

    for i in range(start, len(pages)):
        text = " ".join(pages[i].split())
        unit = UNIT_RE.search(text)
        if unit:  # 表头里的单位；资产负债表分页时，后面几页沿用前面的单位
            scale = 0.001 if unit.group("unit") == "000" else 1.0

        eps_match = EPS_STMT_RE.search(text) if "eps" not in values else None
        if "revenue" not in values and (m := REVENUE_STMT_RE.search(text)):
            values["revenue"] = amounts(m)
        if "profit" not in values and "eps" not in values:  # 只在 EPS 行之前找
            if m := first_profit_match(text[: eps_match.start()] if eps_match else text):
                values["profit"] = amounts(m)
        if eps_match:
            per_unit = 0.01 if eps_match.group("cents") else 1.0
            values["eps"] = [to_number(eps_match.group("a")) * per_unit, to_number(eps_match.group("b")) * per_unit]
            currency = CURRENCY_CODES[eps_match.group("ccy").upper()]
        if "equity" not in values and "eps" in values and (m := EQUITY_STMT_RE.search(text)):
            values["equity"] = amounts(m)  # 资产负债表在损益表之后
        if {"revenue", "profit", "equity", "eps"} <= values.keys():
            break
    missing = {"revenue", "profit", "equity", "eps"} - values.keys()
    if missing:
        raise ValueError(f"没有找到: {sorted(missing)}")
    for i in range(len(pages)):
        if m := WA_SHARES_STMT_RE.search(" ".join(pages[i].split())):
            values["wa_shares"] = [to_number(m.group("a")) / 1000, to_number(m.group("b")) / 1000]  # 千股 -> 百万股
            break
    return currency, values


FORMATS = {"highlights": parse_highlights, "statements": parse_statements}  # 港交所 PDF 公告
FILING_FORMATS = {*FORMATS, "press_release", "kifrs_statements"}  # 加上美股的 HTML 新闻稿和三星的英文财务报表


def parse_announcement(filing: Filing, fmt: str = "highlights", market: Market = HK) -> Announcement:
    """解析一份业绩公告，数值保持公告原样（金额已换算成百万元，EPS 已换算成元；未做拆股调整）。"""
    if fmt in ("press_release", "kifrs_statements"):  # 解析结果自带报告期和是否单季数据
        if fmt == "press_release":
            parsed = parse_press_release(filing.content)
        elif filing.provisional:
            parsed = parse_kifrs_deck(filing.title, filing.content, filing.attachments["previous_bs"])
        else:
            parsed = parse_kifrs(filing.title, filing.content, filing.attachments["bs"], filing.attachments.get("note"))
        basis = "全年" if parsed.period.quarter == 4 and not parsed.quarterly else ""
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
        quarterly = period.quarter == 1  # 一季度公告的“累计”就是单季
    else:
        raise ValueError(f"{filing.url}: 各行的数值个数不符合预期: {values}")
    basis = ""
    if shares is not None:
        # 腾讯的附注里三个月、累计各两个数（本期、去年同期）：取三个月的，对应本季度；
        # 只有两个数的是第一季度（三个月）或全年（第四季度公告没有单独列第四季度的股数）
        basis = "全年" if period.quarter == 4 and (len(shares) == 2 or fmt == "statements") else ""
        values["wa_shares"] = shares[:2]
    return Announcement(period, filing.published, filing.url, currency, quarterly, values, market.key, False, basis)


def split_factor(published: pd.Timestamp, splits: pd.Series) -> float:
    """公告发布之后发生的拆股，需要把公告里的每股数据除以的倍数。"""
    if splits.empty:
        return 1.0
    after = splits[splits.index > published.normalize()]
    return float(after.prod()) if len(after) else 1.0


def adjust_for_splits(announcements: list[Announcement], splits: pd.Series) -> list[Announcement]:
    """把每股数据换算到当前股本口径（与复权股价一致）；金额不受拆股影响。"""
    out = []
    for a in announcements:
        factor = split_factor(a.published, splits)
        values = {
            k: [v / factor for v in vs] if k in PER_SHARE else [v * factor for v in vs] if k in SHARE_COUNTS else vs
            for k, vs in a.values.items()
        }
        out.append(replace(a, values=values))
    return out


ANNOUNCEMENT_COLUMNS = ["published", "period", "table", "currency", *SERIES, "url", "provisional", "shares_basis", "parser_version"]


def announcements_to_frame(announcements: list[Announcement], parser_version: int) -> pd.DataFrame:
    """解析结果的存储格式：每份公告一行，各行数值用分号连接（公告原样数值）。"""
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
            )
        )
    return out


def implied_shares(quarters: dict[str, dict[pd.Period, QuarterValue]], p: pd.Period) -> float | None:
    """当期加权平均股数（百万股）= 当期归属股东净利润 ÷ 当期基本 EPS。"""
    profit, eps = quarters["profit"].get(p), quarters["eps"].get(p)
    if profit is None or eps is None or eps.value == 0:
        return None
    return profit.value / eps.value


@dataclass
class EarningsHistory:
    currency: str
    quarters: dict[str, dict[pd.Period, QuarterValue]]  # series -> 期间 -> 单期值（equity 为期末值）
    ttm: pd.DataFrame  # 每份公告发布后可用的数据，列见 TTM_COLUMNS
    announcements: list[Announcement]
    periods_per_year: int = 4  # 每年发布几期业绩：4（季度）、2（半年）、1（年度）
    ttm_detail: list[dict] = field(default_factory=list)  # 与 ttm 逐行对应：各项数据是怎么算出来的（见 audit.py）
    shares_basis: dict[pd.Period, str] = field(default_factory=dict)  # 各期披露的加权平均股数对应的期间（空 = 本期，“全年”）

    @property
    def step(self) -> int:
        """相邻两期相隔几个季度。"""
        return 4 // self.periods_per_year

    def discrepancies(self) -> list[str]:
        out = []
        for series, quarters in self.quarters.items():
            fmt = "{:.3f}" if series in PER_SHARE else "{:,.0f}"
            for p, q in sorted(quarters.items()):
                if q.max_deviation > tolerance(series, q.value):
                    vals = ", ".join(fmt.format(o.value) + ("(累计数推算)" if o.derived else "") for o in q.observations)
                    out.append(
                        f"{SERIES_LABELS[series]} {period_label(p, self.periods_per_year)} 在不同公告中数值不一致（可能是公司重述）[{vals}]，"
                        f"采用最早公布的 {fmt.format(q.value)}"
                    )
        for a in self.announcements:
            if a.provisional:
                out.append(
                    f"{period_label(a.period, self.periods_per_year)} 的数据来自业绩发布会材料（审阅前；营收、净利润只精确到 0.1 万亿韩元，"
                    f"净资产是估算值），英文财报发布后会自动换成财报数字"
                )
        shares = {p: s for p in sorted(self.quarters["eps"]) if (s := implied_shares(self.quarters, p))}
        periods = list(shares)
        label = lambda p: period_label(p, self.periods_per_year)  # noqa: E731
        for prev, cur in zip(periods, periods[1:]):
            if cur - prev == self.step and abs(shares[cur] / shares[prev] - 1) > SHARES_CHANGE_LIMIT:
                out.append(
                    f"反推股数 {label(prev)} {shares[prev]:,.0f} → {label(cur)} {shares[cur]:,.0f} 百万股，变化超过 5%，请核对"
                )
        return out

    def periods_frame(self, trading_days: pd.DatetimeIndex | None = None) -> pd.DataFrame:
        """逐期明细（按季度或半年）。给出交易日时，同时列出每个数值实际开始使用的交易日。"""
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
        """每份公告发布后可用的数据，带上计算过程：TTM 覆盖的各期数值、公式和中间量。"""
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
        extra["shares_source"] = [{"reported": "公告披露", "derived": "由全年数推算", "implied": "反推"}[r["shares_source"]] for r in self.ttm_detail]
        extra["shares_implied"] = [r["shares_implied"] for r in self.ttm_detail]
        extra["revenue_per_share_formula"] = [
            _division(r["revenue_ttm"], r["shares"], r["revenue_per_share_ttm"]) for r in self.ttm_detail
        ]
        extra["book_value_per_share_formula"] = [_division(r["equity"], r["shares"], r["book_value_per_share"]) for r in self.ttm_detail]
        extra["split_note"] = [
            "；".join(split_notes(r["eps"]) + split_notes(r["non_ifrs_eps"])) for r in self.ttm_detail
        ]
        df = pd.concat([df.reset_index(drop=True), pd.DataFrame(extra)], axis=1)
        return df[[c for c in TTM_FRAME_ORDER if c in df]]


def _division(numerator: float | None, denominator: float | None, result: float | None) -> str:
    if numerator is None or not denominator or result is None:
        return ""
    return f"{num(numerator)} ÷ {num(denominator)} = {num(result)}"


def effective_day(available: pd.Timestamp, trading_days: pd.DatetimeIndex | None):
    """实际开始使用的交易日（日期）；行情里还没有这一天（尚未开市）或没给交易日时为空。"""
    if trading_days is None:
        return None
    day = effective_trading_day(available, trading_days)
    return day.date() if day is not None else None


def infer_periods_per_year(periods: list[pd.Period]) -> int:
    """每年发布几期业绩：有一季度或三季度的公告就是每季度发布，只有二季度（中期）的是每半年发布。"""
    quarters = {p.quarter for p in periods}
    if quarters & {1, 3}:
        return 4
    return 2 if 2 in quarters else 1


def build_history(parsed: list[Announcement], splits: pd.Series | None = None) -> EarningsHistory:
    announcements: dict[pd.Period, Announcement] = {}
    for a in sorted(parsed, key=lambda x: x.available):
        announcements.setdefault(a.period, a)  # 同一报告期的重复公告只取最早一份

    currencies = {a.currency for a in announcements.values()}
    if len(currencies) != 1:
        raise ValueError(f"公告中的 EPS 币种不一致: {currencies}")
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
            touched: list[tuple[pd.Period, float]] = []  # 这份公告里的年初至今累计数
            if series in BALANCES:
                prior_year_end = pd.Period(year=a.period.year - 1, quarter=4, freq="Q")
                for period, value in zip([a.period, prior_year_end], vals):
                    record(series, period, value, a, derived=False)
            elif series in SHARE_COUNTS:  # 公告披露的股数：就是该期的值，不能由累计数相减
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
    """这份公告发布后可用的数据：各项 TTM（最近一年各期之和）、股数、每股营收、每股净资产。

    返回（数值行, 计算过程）。计算过程记录每项 TTM 由哪几期的哪些数值相加得到，供核对。
    """
    last_year = [a.period - k * (4 // per_year) for k in range(per_year)]
    periods = list(reversed(last_year))  # 从早到晚

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
        # 公告只披露了全年数：全年数是各季度的按天加权平均，所以本期（第四季度或下半年）= 全年 × 全年天数 − 前面各期 × 各自天数，再除以本期天数
        parts = _fiscal_year_parts(a.period, reported.value, quarters["wa_shares"], shares_basis, 4 // per_year)
        if parts:
            shares, source = parts["shares"], "derived"
            for x in parts["parts"]:
                x["period"] = period_label(x["period"], per_year)
        else:
            shares, source, basis = implied, "implied", ""  # 前面各期的股数也没有披露，改为反推
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
    """一期的天数；step 为一期含几个季度（半年报的“期”是两个季度）。"""
    return (p.end_time.normalize() - (p - (step - 1)).start_time.normalize()).days + 1


def _fiscal_year_parts(
    p: pd.Period, full_year: float, shares: dict[pd.Period, QuarterValue], basis: dict[pd.Period, str], step: int
) -> dict | None:
    """全年加权平均股数 → 本期（年内最后一期）的加权平均股数。需要年内前面各期披露的股数（本期自己的口径）。"""
    earlier = _earlier_periods(p, step)
    if not earlier or any(e not in shares or basis.get(e, "") for e in earlier):
        return None
    year_days = (pd.Timestamp(year=p.year, month=12, day=31) - pd.Timestamp(year=p.year, month=1, day=1)).days + 1
    parts = [{"period": e, "shares": shares[e].value, "days": _days(e, step)} for e in sorted(earlier)]
    days = _days(p, step)
    value = (full_year * year_days - sum(x["shares"] * x["days"] for x in parts)) / days
    return {"shares": value, "full_year": full_year, "year_days": year_days, "days": days, "parts": parts}


def _earlier_periods(p: pd.Period, step: int) -> list[pd.Period]:
    """同一年内排在 p 之前的各期。step 为相邻两期相隔的季度数。"""
    return [p - j * step for j in range(1, p.quarter // step)]


def _derive_from_ytd(series, quarters, ytd, a, record, step: int) -> None:
    """用年初至今累计数推算尚未知道的单期数：本期 = 累计(本期) - 累计(上一期)，或 - 本年此前各期之和。

    每季度发布的公司，年内各期是各季度；每半年发布的公司，上半年的累计数就是上半年本身，
    下半年 = 全年 - 上半年。
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
    """这份公告里的累计数（含去年同期）也能推出各期的数，作为已有数值的又一个来源，用于核对。"""
    for p, total in touched:
        q = quarters.get(p)
        earlier = _earlier_periods(p, step)
        if q is None or any(o.url == a.url for o in q.observations) or not all(e in quarters for e in earlier):
            continue
        q.observations.append(Observation(total - sum(quarters[e].value for e in earlier), a.url, True))
