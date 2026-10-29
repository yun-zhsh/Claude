"""加载并校验 config.yaml。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Level:
    upper: float  # 分位数上限（含），0-100
    label: str


@dataclass(frozen=True)
class StockConfig:
    symbol: str  # Yahoo Finance 代码，如 0700.HK
    name: str
    metrics: list[str] = field(default_factory=lambda: ["pe"])
    filing_format: str = "highlights"  # 业绩公告的版式，见 filings.FORMATS


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
    from valuation_monitor.filings import FORMATS
    from valuation_monitor.metrics import METRICS

    stocks = []
    for s in raw.get("stocks") or []:
        metrics = [m.lower() for m in s.get("metrics", ["pe"])]
        unknown = [m for m in metrics if m not in METRICS]
        if unknown:
            raise ValueError(f"{s['symbol']}: 未知指标 {unknown}，可用指标: {sorted(METRICS)}")
        filing_format = str(s.get("filing_format", "highlights"))
        if filing_format not in FORMATS:
            raise ValueError(f"{s['symbol']}: 未知的公告版式 {filing_format}，可用版式: {sorted(FORMATS)}")
        stocks.append(
            StockConfig(
                symbol=str(s["symbol"]), name=str(s.get("name", s["symbol"])), metrics=metrics, filing_format=filing_format
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
