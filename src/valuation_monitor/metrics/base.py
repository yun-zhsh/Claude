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


def applicable_record(data: StockData, column: str) -> pd.Series:
    """每个交易日用的是哪一份业绩记录：data.fundamentals 里的行号（当日已公布的、该列有值的最新一份），没有则为 NaN。"""
    f = data.fundamentals.reset_index(drop=True).dropna(subset=[column]).sort_values("available_date", kind="stable")
    days = pd.DataFrame({"date": data.close.index})
    merged = pd.merge_asof(days, f[["available_date", column]].assign(record=f.index), left_on="date", right_on="available_date")
    return pd.Series(merged["record"].to_numpy(), index=data.close.index, dtype=float)


def point_in_time(data: StockData, column: str) -> pd.Series:
    """把财报字段对齐到每个交易日：取当日已公布的最新一期数值。"""
    record = applicable_record(data, column)
    values = data.fundamentals.reset_index(drop=True)[column]
    return pd.Series(values.reindex(record.dropna().astype(int)).to_numpy(), index=record.dropna().index).reindex(data.close.index)


class PriceMultiple(Metric):
    """股价 ÷ 每股财务数据（PE、PS、PB 等）。每股数据按财报币种保存，先换算成股价币种。"""

    column: str  # data.fundamentals 中的每股数据列

    def detail(self, data: StockData) -> pd.DataFrame:
        """每个交易日的计算过程：收盘价、汇率、采用的业绩记录（行号）、每股数据（财报币种和股价币种）、比率。"""
        record = applicable_record(data, self.column)
        per_share = point_in_time(data, self.column)
        converted = per_share * data.fx
        ratio = (data.close / converted).where(converted > 0)  # 亏损或净资产为负时无意义
        return pd.DataFrame(
            {"close": data.close, "fx": data.fx, "record": record, "per_share": per_share, "per_share_converted": converted, "ratio": ratio}
        )

    def compute(self, data: StockData) -> pd.Series:
        return self.detail(data)["ratio"].rename(self.key)
