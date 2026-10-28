"""根据历史分位数判断估值高低。"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from valuation_monitor.config import Level

BAND_PERCENTILES = (10, 30, 50, 70, 90)


@dataclass
class Valuation:
    current: float
    as_of: pd.Timestamp
    percentile: float  # 当前值在回看窗口中的分位数，0-100
    level: str
    window_start: pd.Timestamp
    observations: int
    mean: float
    min: float
    max: float
    bands: dict[int, float]  # 分位数 -> 对应指标值


def classify(percentile: float, levels: list[Level]) -> str:
    for lv in levels:
        if percentile <= lv.upper:
            return lv.label
    return levels[-1].label


def evaluate(series: pd.Series, lookback_years: int, levels: list[Level]) -> Valuation:
    series = series.dropna()
    if series.empty:
        raise ValueError("指标序列为空（可能长期亏损或数据缺失）")
    as_of = series.index[-1]
    window_start = as_of - pd.DateOffset(years=lookback_years)
    window = series[series.index >= window_start]
    current = float(window.iloc[-1])
    percentile = float((window <= current).mean() * 100)
    return Valuation(
        current=current,
        as_of=as_of,
        percentile=percentile,
        level=classify(percentile, levels),
        window_start=window.index[0],
        observations=len(window),
        mean=float(window.mean()),
        min=float(window.min()),
        max=float(window.max()),
        bands={p: float(window.quantile(p / 100)) for p in BAND_PERCENTILES},
    )
