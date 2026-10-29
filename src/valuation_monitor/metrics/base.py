from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from valuation_monitor.data import StockData


class Metric(ABC):
    key: str  # 配置文件里用的名字，如 "pe"
    label: str  # 展示用名字，如 "PE (TTM)"

    @abstractmethod
    def compute(self, data: StockData) -> pd.Series:
        """返回按交易日索引的指标序列；无意义的点（如亏损期的 PE）为 NaN。"""


def point_in_time(data: StockData, column: str) -> pd.Series:
    """把财报字段对齐到每个交易日：取当日已公布的最新一期数值。"""
    f = data.fundamentals.dropna(subset=[column]).sort_values("available_date")
    days = pd.DataFrame({"date": data.close.index})
    merged = pd.merge_asof(days, f[["available_date", column]], left_on="date", right_on="available_date")
    return pd.Series(merged[column].to_numpy(), index=data.close.index)


class PriceMultiple(Metric):
    """股价 ÷ 每股财务数据（PE、PS、PB 等）。每股数据按财报币种保存，先换算成股价币种。"""

    column: str  # data.fundamentals 中的每股数据列

    def compute(self, data: StockData) -> pd.Series:
        per_share = point_in_time(data, self.column) * data.fx
        return (data.close / per_share).where(per_share > 0).rename(self.key)  # 亏损或净资产为负时无意义
