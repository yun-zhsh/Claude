"""各披露渠道共用的公告数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Filing:
    title: str
    published: pd.Timestamp  # 交易所所在地的当地时间（不带时区）
    url: str
    content: bytes  # 公告原文：港交所是 PDF，美国证监会是 HTML
    attachments: dict[str, bytes] = field(default_factory=dict)  # 同一份公告的其他文件（如三星的资产负债表）
