from __future__ import annotations

import pandas as pd

from valuation_monitor.data import StockData
from valuation_monitor.metrics.base import Metric, point_in_time


class PE(Metric):
    key = "pe"
    label = "PE (TTM)"

    def compute(self, data: StockData) -> pd.Series:
        eps = point_in_time(data, "eps_ttm") * data.fx  # 换算成股价币种
        pe = data.close / eps
        return pe.where(eps > 0).rename(self.key)  # 亏损时 PE 无意义
