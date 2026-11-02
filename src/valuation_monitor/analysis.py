"""Judge whether a valuation is high or low from its historical percentile."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from valuation_monitor.config import Level

BAND_PERCENTILES = (10, 30, 50, 70, 90)


@dataclass
class Valuation:
    current: float
    as_of: pd.Timestamp
    percentile: float  # percentile of the current value within the lookback window, 0-100
    level: str
    window_start: pd.Timestamp
    observations: int
    mean: float
    min: float
    max: float
    bands: dict[int, float]  # percentile -> metric value at that percentile


def level_index(percentile: float, levels: list[Level]) -> int:
    """Index of the valuation level (0 = most undervalued)."""
    return next((i for i, lv in enumerate(levels) if percentile <= lv.upper), len(levels) - 1)


def classify(percentile: float, levels: list[Level]) -> str:
    return levels[level_index(percentile, levels)].label


def evaluate(series: pd.Series, lookback_years: int, levels: list[Level]) -> Valuation:
    series = series.dropna()
    if series.empty:
        raise ValueError("The metric series is empty (the company may have been loss-making throughout, or data is missing)")
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
