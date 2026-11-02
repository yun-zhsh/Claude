"""HKEXnews: search for and download the original PDFs of listed companies' earnings announcements.

Downloaded filings are saved in data/filings/<symbol>/ (committed to the repository with the code);
index.json in that folder records the title, publication time and source link of every filing.
Existing PDFs are not downloaded again; when HKEXnews cannot be reached, the locally saved filings are used.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.http import HttpClient

BASE_URL = "https://www1.hkexnews.hk"
# Titles of earnings announcements differ by company, e.g. "ANNOUNCEMENT OF THE RESULTS FOR THE THREE MONTHS ENDED 31 MARCH 2026",
# "INTERIM RESULTS ANNOUNCEMENT FOR THE SIX MONTHS ENDED 30 JUNE 2026"; what they share is RESULTS followed by "ENDED <date>".
# Poll results of shareholder meetings (POLL RESULTS OF ...) have no "ENDED <date>" and are excluded as well.
RESULTS_TITLE_RE = re.compile(r"\bRESULTS\b.*\bENDED \d{1,2} [A-Z]+ \d{4}", re.I)


def stock_code(symbol: str) -> str:
    """0700.HK -> 00700."""
    code, _, market = symbol.upper().partition(".")
    if market != "HK":
        raise ValueError(f"Only Hong Kong stocks are supported, got {symbol}")
    return code.zfill(5)


class HkexProvider:
    def __init__(self, http: HttpClient, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def _stock_id(self, code: str) -> int:
        raw = self.http.get_bytes(
            f"{BASE_URL}/search/prefix.do",
            params={"callback": "callback", "lang": "EN", "type": "A", "name": code, "market": "SEHK"},
            immutable=True,
        ).decode()
        info = json.loads(raw[raw.index("(") + 1 : raw.rindex(")")])
        for s in info["stockInfo"]:
            if s["code"] == code:
                return int(s["stockId"])
        raise RuntimeError(f"HKEXnews does not know the stock code {code}")

    def results_announcements(self, symbol: str, since: datetime) -> list[Filing]:
        """All earnings announcements (English version) published after `since`, in ascending order of publication time."""
        folder = self.archive_dir / symbol.upper()
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        by_url = {e["url"]: e for e in index}

        try:
            found = self._search(symbol, since)
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: cannot reach HKEXnews ({e}); using the {len(index)} saved filings", file=sys.stderr)
            found = [e for e in index if pd.Timestamp(e["published"]) >= pd.Timestamp(since)]

        filings = []
        added = False
        for entry in found:
            saved = by_url.get(entry["url"])
            if saved is None:
                published = pd.Timestamp(entry["published"])
                saved = {**entry, "file": f"{published:%Y-%m-%d}_{entry['url'].rsplit('/', 1)[-1]}"}
                folder.mkdir(parents=True, exist_ok=True)
                (folder / saved["file"]).write_bytes(self.http.get_bytes(saved["url"], cache=False))
                index.append(saved)
                by_url[saved["url"]] = saved
                added = True
            filings.append(
                Filing(
                    title=saved["title"],
                    published=pd.Timestamp(saved["published"]),
                    url=saved["url"],
                    content=(folder / saved["file"]).read_bytes(),
                )
            )

        if added:
            index.sort(key=lambda e: e["published"])
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return sorted(filings, key=lambda f: f.published)

    def _search(self, symbol: str, since: datetime) -> list[dict]:
        data = self.http.get_json(
            f"{BASE_URL}/search/titleSearchServlet.do",
            params={
                "sortDir": 0,
                "sortByOptions": "DateTime",
                "category": 0,
                "market": "SEHK",
                "stockId": self._stock_id(stock_code(symbol)),
                "documentType": -1,
                "fromDate": since.strftime("%Y%m%d"),
                "toDate": datetime.now().strftime("%Y%m%d"),
                "title": "results",
                "searchType": 0,
                "t1code": -2,
                "t2Gcode": -2,
                "t2code": -2,
                "rowRange": 500,
                "lang": "E",
            },
        )
        if data.get("hasNextRow"):
            raise RuntimeError("The HKEXnews search returned more than one page; narrow the date range")

        found = []
        for row in json.loads(data["result"]):
            title = " ".join(row["TITLE"].split())
            if "POLL RESULTS" not in title.upper() and RESULTS_TITLE_RE.search(title):
                published = datetime.strptime(row["DATE_TIME"], "%d/%m/%Y %H:%M")
                found.append({"title": title, "published": published.isoformat(), "url": BASE_URL + row["FILE_LINK"]})
        return found
