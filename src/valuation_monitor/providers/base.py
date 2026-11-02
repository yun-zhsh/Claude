"""Filing data structure shared by all disclosure channels."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Filing:
    title: str
    published: pd.Timestamp  # local time of the exchange (no timezone)
    url: str
    content: bytes  # the filing itself: PDF for HKEX, HTML for the SEC
    attachments: dict[str, bytes] = field(default_factory=dict)  # other files of the same filing (e.g. Samsung's balance sheet)
    provisional: bool = False  # preliminary data (e.g. Samsung earnings call presentation), replaced when the official report is out
    note: str = ""  # explanation of the publication time etc. (e.g. the annual report keeps the express report's publication time)
