"""Samsung Electronics IR website (samsung.com/global/ir): English consolidated financial statements and the earnings call schedule.

For each quarter two original PDFs are taken: the consolidated statement of income (soi) and of financial position (bs), saved in data/filings/<symbol>/.

The statement PDFs carry no publication time, and the English version is uploaded about three weeks after the results first became public, so the
publication time is the date and time of the quarter's Earnings Conference Call announced on the IR "Events" page (recorded for every quarter since 2009,
e.g. "3Q26 Earnings Conference Call, October 29, 2026, 10:00 a.m. KST"). That is when the market first saw the numbers.
Up to 2014 only the date is given and 10:00 is assumed. The calls are all before the Korea Exchange close (15:30), so the effective trading day is the day of the call.
Note: the program reads the English financial statements published later (reviewed by the accountants); where their numbers differ from the unreviewed ones in the call, the statements win.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from valuation_monitor.kifrs import eps_note_pages
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.http import HttpClient

BASE = "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/"
STATEMENT_PAGES = [
    "https://www.samsung.com/global/ir/financial-information/audited-financial-statements/",
    "https://www.samsung.com/global/ir/financial-information/earnings-release/",
]
EVENTS_PAGE = "https://www.samsung.com/global/ir/ir-events-presentations/events/"
STATEMENT_RE = re.compile(r"(?P<year>20\d\d)_con_quarter0(?P<q>[1-4])_(?P<kind>soi|bs|note)(?P<variant>_[a-z0-9]+)?\.pdf")
CALL_RE = re.compile(r"^(?P<q>[1-4])Q ?(?:FY)?(?P<year>\d{2}|\d{4}) Earnings Conference Call")
CALL_TIME_RE = re.compile(r"^(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})(?:, (?P<hour>\d{1,2}):(?P<minute>\d\d) a\.m\. KST)?")
DECK_RE = re.compile(r"(?P<year>20\d\d)_(?P<q>[1-4])Q_conference_eng\w*\.pdf")
DEFAULT_CALL_TIME = "10:00"


def parse_calls(html: str) -> dict[tuple[int, int], pd.Timestamp]:
    """Time of each quarter's earnings call on the Events page (Korea time): {(fiscal year, quarter): time}."""
    lines = [" ".join(t.split()) for t in BeautifulSoup(html, "lxml").get_text("\n").split("\n") if t.strip()]
    calls: dict[tuple[int, int], pd.Timestamp] = {}
    for line, nxt in zip(lines, lines[1:]):
        c, t = CALL_RE.match(line), CALL_TIME_RE.match(nxt)
        if not (c and t):
            continue
        year = int(c.group("year")) + (2000 if len(c.group("year")) == 2 else 0)
        quarter = int(c.group("q"))
        when = pd.Timestamp(f"{t.group('date')} {t.group('hour') or DEFAULT_CALL_TIME.split(':')[0]}:{t.group('minute') or DEFAULT_CALL_TIME.split(':')[1]}")
        quarter_end = pd.Timestamp(year=year, month=quarter * 3, day=1) + pd.offsets.MonthEnd(0)
        if not pd.Timedelta(days=15) <= when - quarter_end <= pd.Timedelta(days=60):  # a few entries on the page have a wrong year
            continue
        calls[(year, quarter)] = when
    return calls


def discover(html_pages: list[str]) -> dict[tuple[int, int], dict[str, str]]:
    """Find the financial statement file links of each quarter on the page: {(year, quarter): {"soi": link, "bs": link}}."""
    statements: dict[tuple[int, int], dict[str, str]] = {}
    for html in html_pages:
        for name in sorted(set(re.findall(r"[\w.-]+\.pdf", html))):
            if m := STATEMENT_RE.fullmatch(name):
                kinds = statements.setdefault((int(m.group("year")), int(m.group("q"))), {})
                # when the same statement has several versions (_new, _1), take the suffixed one: it is a corrected re-upload
                if m.group("kind") not in kinds or m.group("variant"):
                    kinds[m.group("kind")] = BASE + name
    return statements


def discover_decks(html_pages: list[str]) -> dict[tuple[int, int], str]:
    """Links of the earnings call materials (presentation): {(year, quarter): link}."""
    decks = {}
    for html in html_pages:
        for name in sorted(set(re.findall(r"[\w.-]+\.pdf", html))):
            if m := DECK_RE.fullmatch(name):
                decks[(int(m.group("year")), int(m.group("q")))] = BASE + name
    return decks


def previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


class SamsungIrProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def results_announcements(self, symbol: str, since: datetime) -> list[Filing]:
        """All quarterly results whose call is after `since`, in ascending order of publication time."""
        folder = self.archive_dir / symbol.upper()
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        known = {(e["year"], e["quarter"]) for e in index if not e.get("provisional")}

        try:
            added = self._fetch_new(symbol, since, folder, index, known)
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: cannot reach the Samsung IR website ({e}); using the {len(index)} saved reports", file=sys.stderr)
            added = False
        if added:
            index.sort(key=lambda e: e["published"])
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        since_ts = pd.Timestamp(since)
        by_quarter = {(e["year"], e["quarter"]): e for e in index}
        filings = []
        for e in index:
            if pd.Timestamp(e["published"]) < since_ts:
                continue
            title = f"Samsung Electronics {e['quarter']}Q {e['year']} consolidated financial statements"
            if e.get("provisional"):
                prev = by_quarter.get(previous_quarter(e["year"], e["quarter"]))
                if prev is None or prev.get("provisional"):
                    continue
                filings.append(
                    Filing(
                        title=title.replace("consolidated financial statements", "earnings call presentation (provisional)"),
                        published=pd.Timestamp(e["published"]),
                        url=e["url"],
                        content=(folder / e["files"]["deck"]).read_bytes(),
                        attachments={"previous_bs": (folder / prev["files"]["bs"]).read_bytes()},
                        provisional=True,
                    )
                )
            else:
                attachments = {"bs": (folder / e["files"]["bs"]).read_bytes()}
                if e["files"].get("note"):
                    attachments["note"] = (folder / e["files"]["note"]).read_bytes()  # EPS note page: weighted average shares
                filings.append(
                    Filing(
                        title=title,
                        published=pd.Timestamp(e["published"]),
                        url=e["url"],
                        content=(folder / e["files"]["soi"]).read_bytes(),
                        attachments=attachments,
                    )
                )
        return filings

    def _save_eps_note(self, folder: Path, year: int, quarter: int, kinds: dict[str, str]) -> str | None:
        """Download the note PDF and save only the page with the EPS note; return None if the file is missing or the note cannot be found (shares are then implied)."""
        if "note" not in kinds:
            return None
        cut = eps_note_pages(self.http.get_bytes(kinds["note"], cache=False))
        if cut is None:
            return None
        name = f"{year}Q{quarter}_{kinds['note'].rsplit('/', 1)[-1].replace('.pdf', '')}_eps.pdf"
        (folder / name).write_bytes(cut)
        return name

    def _fetch_new(self, symbol, since, folder, index, known) -> bool:
        pages = [self.http.get_bytes(url).decode("utf-8", "replace") for url in STATEMENT_PAGES]
        statements = discover(pages)
        decks = discover_decks(pages)
        calls = parse_calls(self.http.get_bytes(EVENTS_PAGE).decode("utf-8", "replace"))
        added = False

        def too_old(year, quarter):
            quarter_end = pd.Timestamp(year=year, month=quarter * 3, day=1) + pd.offsets.MonthEnd(0)
            return quarter_end + pd.Timedelta(days=60) < pd.Timestamp(since)  # the call cannot be inside the lookback range

        for (year, quarter), kinds in sorted(statements.items()):
            if (year, quarter) in known or {"soi", "bs"} - kinds.keys() or too_old(year, quarter):
                continue
            published = calls.get((year, quarter))
            if published is None:
                print(f"{symbol}: {year}Q{quarter} has no earnings call date on the IR Events page, so the publication time is unknown; skipped", file=sys.stderr)
                continue
            if published < pd.Timestamp(since):
                continue
            # the official report is out: replaces the preliminary call material data used so far
            for old in [e for e in index if (e["year"], e["quarter"]) == (year, quarter) and e.get("provisional")]:
                (folder / old["files"]["deck"]).unlink(missing_ok=True)
                index.remove(old)
            folder.mkdir(parents=True, exist_ok=True)
            files = {}
            for kind in ("soi", "bs"):
                files[kind] = f"{year}Q{quarter}_{kinds[kind].rsplit('/', 1)[-1]}"
                (folder / files[kind]).write_bytes(self.http.get_bytes(kinds[kind], cache=False))
            files["note"] = self._save_eps_note(folder, year, quarter, kinds)
            index.append({"year": year, "quarter": quarter, "published": f"{published:%Y-%m-%dT%H:%M:%S}", "url": kinds["soi"], "files": files})
            known.add((year, quarter))
            added = True
            print(f"{symbol}: new report {year}Q{quarter}, earnings call {published}", file=sys.stderr)

        # reports saved earlier have no EPS note yet (the weighted average shares disclosed in the filing): fetch it
        for e in index:
            if not e.get("provisional") and "note" not in e["files"] and (e["year"], e["quarter"]) in statements:
                e["files"]["note"] = self._save_eps_note(folder, e["year"], e["quarter"], statements[(e["year"], e["quarter"])])
                added = True

        # latest quarter without an English report yet: use the data in the earnings call material for now (available on the day of the call)
        have_provisional = {(e["year"], e["quarter"]) for e in index if e.get("provisional")}
        for (year, quarter), deck in sorted(decks.items()):
            published = calls.get((year, quarter))
            if (year, quarter) in known | have_provisional or published is None or published < pd.Timestamp(since) or too_old(year, quarter):
                continue
            if previous_quarter(year, quarter) not in known:
                continue  # the equity estimate needs the previous quarter's report
            name = f"{year}Q{quarter}_{deck.rsplit('/', 1)[-1]}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(self.http.get_bytes(deck, cache=False))
            index.append(
                {"year": year, "quarter": quarter, "published": f"{published:%Y-%m-%dT%H:%M:%S}", "url": deck, "files": {"deck": name}, "provisional": True}
            )
            added = True
            print(f"{symbol}: the {year}Q{quarter} English statements are not out yet; using the earnings call presentation ({published}) for now", file=sys.stderr)
        return added
