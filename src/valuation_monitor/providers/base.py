"""Filing data structure shared by all disclosure channels."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True)
class Release:
    """A record whose figures came from a document published later than the record's publication time.

    The publication time of a record is when its figures first became public (an express report, an earnings call presentation); when the
    official document came out later, its more exact figures replaced the first ones but the time stayed. For asking "what was public on day X"
    that matters: between `first_published` and `public_by` the document the record links to may not have existed yet.
    """

    first_published: pd.Timestamp  # when the figures first became public
    first_kind: str  # what was public then, e.g. "express report"
    first_url: str | None  # that document, if it is on record
    document_published: pd.Timestamp | None  # when the document the figures come from was published; None if not recorded
    public_by: pd.Timestamp  # the document is public from this time on: document_published, or an upper bound when that is not recorded


def save_index(path: Path, index: list[dict]) -> bool:
    """Write a provider's index.json only if its content changed. Returns whether it was written.

    Reading the saved filings must work where nothing can be written (the read-only bucket mount of the MCP service), and an unchanged
    index is not a change to commit either.
    """
    text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True
