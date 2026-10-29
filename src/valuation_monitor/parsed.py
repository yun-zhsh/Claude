"""各种公告版式的解析结果。"""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd


class Parsed(NamedTuple):
    currency: str
    values: dict[str, list[float]]  # series -> 本期、去年同期、（上一期）；equity 为本期末、上年末
    period: pd.Period  # 报告季度
    quarterly: bool = True  # False 表示是年初至今累计数（如全年）
