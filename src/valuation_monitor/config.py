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


@dataclass(frozen=True)
class Config:
    stocks: list[StockConfig]
    lookback_years: int = 10
    levels: list[Level] = field(default_factory=list)


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
    from valuation_monitor.metrics import METRICS

    stocks = []
    for s in raw.get("stocks") or []:
        metrics = [m.lower() for m in s.get("metrics", ["pe"])]
        unknown = [m for m in metrics if m not in METRICS]
        if unknown:
            raise ValueError(f"{s['symbol']}: 未知指标 {unknown}，可用指标: {sorted(METRICS)}")
        stocks.append(StockConfig(symbol=str(s["symbol"]), name=str(s.get("name", s["symbol"])), metrics=metrics))
    if not stocks:
        raise ValueError("配置中没有任何股票（stocks 为空）")

    levels = [Level(float(lv["upper"]), str(lv["label"])) for lv in raw.get("levels") or []]
    levels = sorted(levels, key=lambda lv: lv.upper) or DEFAULT_LEVELS
    if levels[-1].upper < 100:
        raise ValueError("levels 最后一档的 upper 必须为 100")

    return Config(stocks=stocks, lookback_years=int(raw.get("lookback_years", 10)), levels=levels)
