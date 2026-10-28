"""把各数据源的原始数据整理成指标计算需要的形式。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from valuation_monitor.config import Config, StockConfig
from valuation_monitor.providers.eastmoney import EastmoneyProvider
from valuation_monitor.providers.yahoo import YahooProvider


@dataclass
class StockData:
    stock: StockConfig
    close: pd.Series  # 交易日收盘价，股价币种
    price_currency: str
    fx: pd.Series  # 与 close 同索引：1 单位财报币种 = 多少股价币种
    fundamentals: pd.DataFrame  # report_date, available_date, eps_ttm（已按拆股调整，财报币种）


def adjust_for_splits(fundamentals: pd.DataFrame, splits: pd.Series, per_share_cols: list[str]) -> pd.DataFrame:
    """把拆股前报告期的每股数据换算到当前股本口径（与 Yahoo 复权股价一致）。"""
    out = fundamentals.copy()
    for split_date, ratio in splits.items():
        before = out["report_date"] < split_date
        out.loc[before, per_share_cols] = out.loc[before, per_share_cols] / ratio
    return out


def add_available_date(fundamentals: pd.DataFrame, annual_lag_days: int, interim_lag_days: int) -> pd.DataFrame:
    """估算每期财报对市场可见的日期，避免在历史估值中用到当时还没公布的数据。"""
    out = fundamentals.copy()
    lag = out["is_annual"].map({True: annual_lag_days, False: interim_lag_days})
    out["available_date"] = out["report_date"] + pd.to_timedelta(lag, unit="D")
    return out


class DataLoader:
    def __init__(self, config: Config, yahoo: YahooProvider, eastmoney: EastmoneyProvider):
        self.config = config
        self.yahoo = yahoo
        self.eastmoney = eastmoney

    def load(self, stock: StockConfig) -> StockData:
        # 多取一年数据，保证回看窗口起点之前就有可用的 TTM 数据
        start = datetime.now() - pd.DateOffset(years=self.config.lookback_years + 1)
        prices = self.yahoo.price_history(stock.symbol, start)

        if stock.report_currency == prices.currency:
            fx = pd.Series(1.0, index=prices.close.index)
        else:
            raw_fx = self.yahoo.fx_history(stock.report_currency, prices.currency, start)
            fx = raw_fx.reindex(raw_fx.index.union(prices.close.index)).sort_index().ffill()
            fx = fx.reindex(prices.close.index)

        fundamentals = self.eastmoney.financial_indicators(stock.symbol)
        fundamentals = adjust_for_splits(fundamentals, prices.splits, ["eps_ttm"])
        fundamentals = add_available_date(fundamentals, self.config.annual_lag_days, self.config.interim_lag_days)

        return StockData(
            stock=stock,
            close=prices.close,
            price_currency=prices.currency,
            fx=fx,
            fundamentals=fundamentals,
        )
