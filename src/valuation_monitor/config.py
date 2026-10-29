"""加载并校验 config.yaml。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Level:
    upper: float  # 分位数上限（含），0-100
    label: str


FILING_SOURCES = {"HK": "港交所披露易", "US": "美国证监会 EDGAR"}


@dataclass(frozen=True)
class StockConfig:
    symbol: str  # Yahoo Finance 代码，如 0700.HK、JD
    name: str
    metrics: list[str] = field(default_factory=lambda: ["pe"])
    filing_format: str = "highlights"  # 业绩公告的版式，见 filings.FILING_FORMATS
    market: str = "HK"  # 上市地：HK（港交所披露易）或 US（美国证监会 EDGAR）
    cik: int | None = None  # 美股公司在美国证监会的编号（Central Index Key）

    @property
    def filing_source(self) -> str:
        return FILING_SOURCES[self.market]

    @property
    def cross_checks_prices(self) -> bool:
        """是否有两个以上来源的股价可以互相核对（港股有腾讯证券和 Yahoo，美股只有 Yahoo）。"""
        return self.market == "HK"


DEFAULT_LOOKBACK_OPTIONS = [10, 5, 3, 2, 1]


@dataclass(frozen=True)
class Config:
    stocks: list[StockConfig]
    lookback_years: int = 10  # 默认选中的回看年数
    levels: list[Level] = field(default_factory=list)
    lookback_options: list[int] = field(default_factory=lambda: list(DEFAULT_LOOKBACK_OPTIONS))  # 网页上可选的回看年数

    @property
    def max_lookback(self) -> int:
        """最长的回看年数，决定需要准备多少年的行情和业绩数据。"""
        return max(self.lookback_options)


DEFAULT_LEVELS = [
    Level(10, "极度低估"),
    Level(30, "低估"),
    Level(70, "合理"),
    Level(90, "高估"),
    Level(100, "极度高估"),
]


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return parse_config(raw)


def parse_config(raw: dict) -> Config:
    from valuation_monitor.filings import FILING_FORMATS
    from valuation_monitor.market_calendar import MARKETS
    from valuation_monitor.metrics import METRICS

    stocks = []
    for s in raw.get("stocks") or []:
        metrics = [m.lower() for m in s.get("metrics", ["pe"])]
        unknown = [m for m in metrics if m not in METRICS]
        if unknown:
            raise ValueError(f"{s['symbol']}: 未知指标 {unknown}，可用指标: {sorted(METRICS)}")
        symbol = str(s["symbol"])
        market = str(s.get("market", "HK" if symbol.upper().endswith(".HK") else "US")).upper()
        if market not in MARKETS:
            raise ValueError(f"{symbol}: 未知的市场 {market}，可用市场: {sorted(MARKETS)}")
        # 港股的业绩来自港交所公告（PDF），美股的来自美国证监会 EDGAR 的新闻稿（HTML）
        default_format = "press_release" if market == "US" else "highlights"
        filing_format = str(s.get("filing_format", default_format))
        if filing_format not in FILING_FORMATS:
            raise ValueError(f"{symbol}: 未知的公告版式 {filing_format}，可用版式: {sorted(FILING_FORMATS)}")
        if (filing_format == "press_release") != (market == "US"):
            raise ValueError(f"{symbol}: 公告版式 {filing_format} 与市场 {market} 不匹配（美股用 press_release，港股用其他版式）")
        cik = int(s["cik"]) if s.get("cik") is not None else None
        if market == "US" and cik is None:
            raise ValueError(f"{symbol}: 美股需要配置 cik（公司在美国证监会的编号）")
        stocks.append(
            StockConfig(
                symbol=symbol,
                name=str(s.get("name", symbol)),
                metrics=metrics,
                filing_format=filing_format,
                market=market,
                cik=cik,
            )
        )
    if not stocks:
        raise ValueError("配置中没有任何股票（stocks 为空）")

    levels = [Level(float(lv["upper"]), str(lv["label"])) for lv in raw.get("levels") or []]
    levels = sorted(levels, key=lambda lv: lv.upper) or DEFAULT_LEVELS
    if levels[-1].upper < 100:
        raise ValueError("levels 最后一档的 upper 必须为 100")

    options = sorted({int(y) for y in raw.get("lookback_options") or DEFAULT_LOOKBACK_OPTIONS}, reverse=True)
    if options[-1] < 1:
        raise ValueError("lookback_options 里的年数必须是正整数")
    default = int(raw.get("lookback_years", options[0]))
    if default not in options:
        raise ValueError(f"lookback_years={default} 必须是 lookback_options {options} 之一")
    return Config(stocks=stocks, lookback_years=default, levels=levels, lookback_options=options)
