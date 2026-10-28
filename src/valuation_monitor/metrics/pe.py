from __future__ import annotations

import pandas as pd

from valuation_monitor.data import StockData
from valuation_monitor.metrics.base import Metric, point_in_time


class PE(Metric):
    key = "pe"
    label = "PE (TTM)"
    eps_column = "eps_ttm"

    def compute(self, data: StockData) -> pd.Series:
        eps = point_in_time(data, self.eps_column) * data.fx  # 换算成股价币种
        pe = data.close / eps
        return pe.where(eps > 0).rename(self.key)  # 亏损时 PE 无意义


class NonIfrsPE(PE):
    """用公司披露的 Non-IFRS 利润计算，剔除投资收益、股权激励等非经营项目。"""

    key = "pe_non_ifrs"
    label = "PE Non-IFRS (TTM)"
    eps_column = "non_ifrs_eps_ttm"
