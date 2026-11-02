"""Parse results of the various filing layouts."""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd


class Parsed(NamedTuple):
    currency: str
    values: dict[str, list[float]]  # series -> this period, same period last year, (previous period); equity: end of this period, end of last year
    period: pd.Period  # reporting quarter
    quarterly: bool = True  # False means year-to-date cumulative figures (e.g. the full year)
