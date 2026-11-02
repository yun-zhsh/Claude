from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from valuation_monitor.data import StockData


class Metric(ABC):
    key: str  # name used in the config file, e.g. "pe"
    label: str  # display name, e.g. "PE (TTM)"

    @abstractmethod
    def compute(self, data: StockData) -> pd.Series:
        """Return the metric series indexed by trading day; meaningless points (e.g. PE in a loss period) are NaN."""


def applicable_record(data: StockData, column: str) -> pd.Series:
    """Which earnings record is used on each trading day: the row number in data.fundamentals (the latest one already published that day with a value in the column), NaN if none."""
    f = data.fundamentals.reset_index(drop=True).dropna(subset=[column]).sort_values("available_date", kind="stable")
    days = pd.DataFrame({"date": data.close.index})
    merged = pd.merge_asof(days, f[["available_date", column]].assign(record=f.index), left_on="date", right_on="available_date")
    return pd.Series(merged["record"].to_numpy(), index=data.close.index, dtype=float)


def point_in_time(data: StockData, column: str) -> pd.Series:
    """Align a reporting field to every trading day: the value of the latest period published by that day."""
    record = applicable_record(data, column)
    values = data.fundamentals.reset_index(drop=True)[column]
    return pd.Series(values.reindex(record.dropna().astype(int)).to_numpy(), index=record.dropna().index).reindex(data.close.index)


class PriceMultiple(Metric):
    """Price / per-share financial figure (PE, PS, PB, ...). Per-share figures are stored in the reporting currency and first converted to the price currency."""

    column: str  # per-share column in data.fundamentals

    def detail(self, data: StockData) -> pd.DataFrame:
        """The calculation on every trading day: close, FX rate, earnings record used (row number), per-share value (reporting and price currency), ratio."""
        record = applicable_record(data, self.column)
        per_share = point_in_time(data, self.column)
        converted = per_share * data.fx
        ratio = (data.close / converted).where(converted > 0)  # meaningless for losses or negative equity
        return pd.DataFrame(
            {"close": data.close, "fx": data.fx, "record": record, "per_share": per_share, "per_share_converted": converted, "ratio": ratio}
        )

    def compute(self, data: StockData) -> pd.Series:
        return self.detail(data)["ratio"].rename(self.key)
