from __future__ import annotations

from valuation_monitor.metrics.base import PriceMultiple


class PE(PriceMultiple):
    key = "pe"
    label = "PE (TTM)"
    column = "eps_ttm"


class NonIfrsPE(PriceMultiple):
    """用公司披露的 Non-IFRS 利润计算，剔除投资收益、股权激励等非经营项目。"""

    key = "pe_non_ifrs"
    label = "PE Non-IFRS (TTM)"
    column = "non_ifrs_eps_ttm"


class NonGaapPE(PriceMultiple):
    """用公司披露的 Non-GAAP 利润计算（美股公司的口径），剔除股权激励、投资损益等项目。"""

    key = "pe_non_gaap"
    label = "PE Non-GAAP (TTM)"
    column = "non_ifrs_eps_ttm"


class PS(PriceMultiple):
    """市销率：股价 ÷ 每股营收（最近 4 个季度营收之和 ÷ 最新一季的加权平均股数）。"""

    key = "ps"
    label = "PS (TTM)"
    column = "revenue_per_share_ttm"


class PB(PriceMultiple):
    """市净率：股价 ÷ 每股净资产（最新一期末归属股东净资产 ÷ 最新一季的加权平均股数）。"""

    key = "pb"
    label = "PB"
    column = "book_value_per_share"
