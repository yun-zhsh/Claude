from __future__ import annotations

from valuation_monitor.metrics.base import PriceMultiple


class PE(PriceMultiple):
    key = "pe"
    label = "PE (TTM)"
    column = "eps_ttm"


class NonIfrsPE(PriceMultiple):
    """Computed from the Non-IFRS profit the company discloses, excluding non-operating items such as investment gains and share-based compensation."""

    key = "pe_non_ifrs"
    label = "PE Non-IFRS (TTM)"
    column = "non_ifrs_eps_ttm"


class NonGaapPE(PriceMultiple):
    """Computed from the Non-GAAP profit the company discloses (the US convention), excluding items such as share-based compensation and investment gains or losses."""

    key = "pe_non_gaap"
    label = "PE Non-GAAP (TTM)"
    column = "non_ifrs_eps_ttm"


class PS(PriceMultiple):
    """Price-to-sales: price / revenue per share (sum of the last 4 quarters of revenue / weighted average shares of the latest quarter)."""

    key = "ps"
    label = "PS (TTM)"
    column = "revenue_per_share_ttm"


class PB(PriceMultiple):
    """Price-to-book: price / book value per share (equity attributable to owners at the end of the latest period / weighted average shares of the latest quarter)."""

    key = "pb"
    label = "PB"
    column = "book_value_per_share"
