"""SEC EDGAR: search for and download the original quarterly earnings press releases of US-listed (foreign private issuer) companies.

Foreign private issuers do not file 10-Q; earnings are furnished on Form 6-K, whose body is the press release in exhibit 99.1 (HTML).
Flow: list all 6-Ks from the submissions API -> look at the file list of each 6-K and pick the press release exhibit of a plausible size ->
only a title like "... Announces ... Results" is an earnings announcement.

Downloaded earnings filings are saved in data/filings/<symbol>/ (committed to the repository with the code); index.json there
records every 6-K that has been checked (including those that are not earnings), so they are not checked again. When EDGAR cannot be reached the locally saved filings are used.

EDGAR requires every request to carry a User-Agent that identifies a contact, and allows at most 10 requests per second.
The contact is not written in the code but provided through the SEC_USER_AGENT environment variable, e.g. "valuation-monitor your-email".
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.http import HttpClient

USER_AGENT_ENV = "SEC_USER_AGENT"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{name}"
REQUEST_INTERVAL = 0.2  # seconds; the official limit is 10 requests per second
# Size of earnings press release exhibits: too small are notices, too large are annual reports or prospectuses (some 6-K exhibits exceed 15MB)
MIN_EXHIBIT_BYTES = 100_000
MAX_EXHIBIT_BYTES = 3_000_000
EXHIBIT_RE = re.compile(r"ex-?99", re.I)
# Title at the start of the press release, e.g. "JD.com Announces Second Quarter 2026 Results", "... Announces Fourth Quarter and Full Year 2025 Results"
EXHIBIT_LABEL_RE = re.compile(r"^.*?\bExhibi?t? ?99\.\d\s+", re.I)
RESULTS_HEADLINE_RE = re.compile(r"\bannounces?\b.{0,80}?\bresults\b", re.I)


class SecUserAgentMissing(RuntimeError):
    pass


def make_http_client(cache_dir: str | Path = ".cache", ttl_seconds: int = 12 * 3600, refresh: bool = False) -> HttpClient:
    agent = os.environ.get(USER_AGENT_ENV, "").strip()
    if not agent:
        raise SecUserAgentMissing(
            f"The SEC requires you to identify yourself: set the environment variable {USER_AGENT_ENV} to \"program-name contact-email\", e.g. valuation-monitor you@example.com"
        )
    return HttpClient(cache_dir, ttl_seconds, refresh, user_agent=agent, min_interval=REQUEST_INTERVAL)


def to_new_york(accepted: str) -> pd.Timestamp:
    """acceptanceDateTime (UTC, e.g. 2026-08-13T10:05:29.000Z) -> New York local time (timezone-naive)."""
    return pd.Timestamp(accepted).tz_convert("America/New_York").tz_localize(None).floor("s")


def headline(html: bytes) -> str | None:
    """The title at the start of the press release; None if it is not an earnings press release."""
    from bs4 import BeautifulSoup

    text = " ".join(BeautifulSoup(html[:200_000], "lxml").get_text(" ").split())
    text = EXHIBIT_LABEL_RE.sub("", text, count=1)[:1500]  # strip labels such as "EX-99.1 Exhibit 99.1" from the file header
    m = RESULTS_HEADLINE_RE.search(text)
    if not m:
        return None
    return text[: m.end()].strip() if m.start() <= 60 else text[m.start() - 40 : m.end()].strip()


class SecProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def results_announcements(self, symbol: str, cik: int, since: datetime) -> list[Filing]:
        """All earnings press releases published after `since`, in ascending order of publication time."""
        folder = self.archive_dir / symbol.upper()
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        known = {e["accession"] for e in index}

        try:
            if self.http is None:
                raise SecUserAgentMissing(f"The environment variable {USER_AGENT_ENV} is not set")
            added = self._examine_new(symbol, cik, since, folder, index, known)
        except Exception as e:
            if not any(e_["results"] for e_ in index):
                raise
            print(f"{symbol}: cannot reach the SEC ({e}); using the {sum(x['results'] for x in index)} saved filings", file=sys.stderr)
            added = True  # the part already checked is written to the index as usual

        if added:
            index.sort(key=lambda e: e["published"])
            folder.mkdir(parents=True, exist_ok=True)
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        since_ts = pd.Timestamp(since)
        return [
            Filing(e["title"], pd.Timestamp(e["published"]), e["url"], (folder / e["file"]).read_bytes())
            for e in index
            if e["results"] and pd.Timestamp(e["published"]) >= since_ts
        ]

    def _six_k_list(self, cik: int) -> list[dict]:
        data = self.http.get_json(SUBMISSIONS_URL.format(name=f"CIK{cik:010d}.json"))
        blocks = [data["filings"]["recent"]]
        for extra in data["filings"].get("files", []):  # older records are split into other files
            blocks.append(self.http.get_json(SUBMISSIONS_URL.format(name=extra["name"])))
        out = []
        for b in blocks:
            for i, form in enumerate(b["form"]):
                if form == "6-K":
                    out.append({"accession": b["accessionNumber"][i], "published": to_new_york(b["acceptanceDateTime"][i])})
        return sorted(out, key=lambda x: x["published"])

    def _examine_new(self, symbol, cik, since, folder, index, known) -> bool:
        added = False
        for item in self._six_k_list(cik):
            if item["accession"] in known or item["published"] < pd.Timestamp(since):
                continue
            entry = self._examine(cik, item, folder)
            index.append(entry)
            known.add(entry["accession"])
            added = True
            if entry["results"]:
                print(f"{symbol}: new results release {entry['published']} {entry['title']}", file=sys.stderr)
        return added

    def _examine(self, cik: int, item: dict, folder: Path) -> dict:
        accession = item["accession"]
        folder_name = accession.replace("-", "")
        entry = {"accession": accession, "published": f"{item['published']:%Y-%m-%dT%H:%M:%S}", "results": False}
        listing = json.loads(
            self.http.get_bytes(ARCHIVE_URL.format(cik=cik, accession=folder_name, name="index.json"), immutable=True)
        )
        for f in listing["directory"]["item"]:
            size = int(f["size"] or 0)
            name = f["name"]
            if not (EXHIBIT_RE.search(name) and name.lower().endswith((".htm", ".html"))):
                continue
            if not MIN_EXHIBIT_BYTES <= size <= MAX_EXHIBIT_BYTES:
                continue
            url = ARCHIVE_URL.format(cik=cik, accession=folder_name, name=name)
            html = self.http.get_bytes(url, cache=False)
            title = headline(html)
            if title:
                file = f"{item['published']:%Y-%m-%d}_{name}"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / file).write_bytes(html)
                return {**entry, "results": True, "title": title, "url": url, "file": file}
        return entry
