"""CNINFO (cninfo.com.cn, the information disclosure platform designated by the China Securities Regulatory Commission):
original PDFs of the periodic reports and express reports of A-share companies.

Downloaded files are saved in data/filings/<symbol>/; index.json in that folder records the title, publication time
and source link of every filing. To keep the repository small, only the pages the parser needs are kept (key accounting
data, consolidated income statement, consolidated balance sheet), unchanged.

Publication time:
  - The CNINFO announcement list only has dates, and it records filings made after the close at 00:00 of the next day
    (a Friday-evening filing shows up as Saturday 00:00). So the listed date is treated as "before the open that day":
    the effective trading day is that day or the first trading day after it, which matches the real publication time.
  - Since October 2020 the upload time of the PDF (HTTP Last-Modified) is the actual publication time (Hikvision usually
    publishes on Friday between 16:00 and 22:00, after the close), so it is used when available. Older files were all
    migrated in September 2020, so their upload time is meaningless and is not used.

Express reports: the annual express report comes out before the annual report (January-February) and is the first time
the full-year results are public. Once the annual report is out, its exact numbers replace the express numbers, but the
publication time stays that of the express report (the market first saw the full-year results that day); where the
two differ, the annual report wins.

The Chinese-language patterns that recognise the titles and the text are in cn_patterns.py.
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
from pypdf import PdfReader, PdfWriter

from valuation_monitor import cn_patterns as cn
from valuation_monitor.providers.base import Filing, Release, save_index
from valuation_monitor.providers.http import HttpClient

BASE = "https://www.cninfo.com.cn"
STATIC = "https://static.cninfo.com.cn/"
CST = timezone(timedelta(hours=8))
REPORT_CATEGORIES = "category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh;"
TRUSTED_UPLOAD_TIME_FROM = pd.Timestamp("2020-10-01")  # files before this date were migrated in September 2020; their upload time is not the publication time
REQUEST_INTERVAL = 0.5
UPLOAD_TIME_BASIS = "PDF upload time"
LISTING_DATE_BASIS = "CNINFO listing date (date only; filings made after the close are listed at 00:00 of the next day)"
KIND_TITLES = {1: "Q1 report", 2: "H1 report", 3: "Q3 report", 4: "annual report"}


def classify(title: str) -> tuple[str, int, int] | None:
    """CNINFO announcement title -> (kind "report"/"express", year, quarter); None if it is not a filing we want."""
    title = title.replace("<em>", "").replace("</em>", "").strip()
    if any(word in title for word in cn.EXCLUDED_TITLE_WORDS):
        return None
    if m := cn.TITLE_RE.match(title):
        return "report", int(m.group("year")), cn.QUARTER_OF[m.group("kind")]
    if m := cn.EXPRESS_RE.match(title):
        return "express", int(m.group("year")), cn.QUARTER_OF[m.group("kind")]
    return None


def english_title(kind: str, year: int, quarter: int) -> str:
    """The title used everywhere in the program: "2026 Q3 report", "2025 annual express report"."""
    if kind == "express":
        return f"{year} {'H1' if quarter == 2 else 'annual'} express report"
    return f"{year} {KIND_TITLES[quarter]}"


def exchange_column(code: str) -> str:
    return "sse" if code.startswith("6") else "szse"


def listing_time(ms: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.fromtimestamp(ms / 1000, CST).replace(tzinfo=None))


def published_time(listed: pd.Timestamp, last_modified: str | None) -> tuple[pd.Timestamp, str]:
    """Publication time and how it was determined."""
    if last_modified:
        uploaded = pd.Timestamp(parsedate_to_datetime(last_modified).astimezone(CST).replace(tzinfo=None)).floor("s")
        # The upload time should be the day before the listed date (after-close filings are listed the next day) or the same day
        if uploaded >= TRUSTED_UPLOAD_TIME_FROM and timedelta(0) <= listed - uploaded.normalize() <= timedelta(days=1):
            return uploaded, UPLOAD_TIME_BASIS
    return listed, LISTING_DATE_BASIS


def excerpt_pages(pdf: bytes) -> bytes:
    """Keep only the pages the parser needs (key data table, income statement, balance sheet, plus the next page of each); the pages are unchanged."""
    reader = PdfReader(io.BytesIO(pdf))
    if len(reader.pages) <= 6:
        return pdf  # an express report has two pages; keep the whole file
    texts = [" ".join((p.extract_text() or "").split()) for p in reader.pages]
    wanted: set[int] = set()
    for i, t in enumerate(texts):
        wanted_page = cn.is_wanted_page(t)
        if wanted_page:
            wanted.update({i, i + 1})
    writer = PdfWriter()
    for i in sorted(w for w in wanted if w < len(texts)):
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class CninfoProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def results_announcements(self, symbol: str, since: datetime) -> list[Filing]:
        """Periodic reports published since `since`, plus the express reports whose annual report is not out yet, oldest first."""
        folder = self.archive_dir / symbol.upper()
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        try:
            added = self._fetch_new(symbol, since, folder, index)
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: cannot reach CNINFO ({e}); using the {len(index)} saved filings", file=sys.stderr)
            added = False
        if added:
            index.sort(key=lambda e: (e["published"], e["id"]))
            save_index(index_path, index)

        reports = {(e["year"], e["quarter"]): e for e in index if e["kind"] == "report"}
        since_ts = pd.Timestamp(since)
        filings = []
        for e in index:
            key = (e["year"], e["quarter"])
            if e["kind"] == "express" and key in reports:
                continue  # the official report is out, so the express report is superseded
            published, note = pd.Timestamp(e["published"]), ""
            if e["kind"] == "report":
                express = next((x for x in index if x["kind"] == "express" and (x["year"], x["quarter"]) == key), None)
                if express is not None and pd.Timestamp(express["published"]) < published:
                    # The numbers were first public on the express date; the annual report's exact numbers replace the express numbers but keep the express publication time
                    note = (
                        f"Numbers from the {e['title']} (published {published:%Y-%m-%d %H:%M}); publication time kept from the express report "
                        f"({pd.Timestamp(express['published']):%Y-%m-%d %H:%M}); where the express numbers differ, the annual report wins"
                    )
                    published = pd.Timestamp(express["published"])
            if published < since_ts:
                continue
            filings.append(
                Filing(
                    title=e["title"],
                    published=published,
                    url=e["url"],
                    content=(folder / e["file"]).read_bytes(),
                    provisional=e["kind"] == "express",
                    note=note,
                )
            )
        return sorted(filings, key=lambda f: f.published)

    def release_info(self, symbol: str) -> dict[str, Release]:
        """For each periodic report that replaced an earlier express report: what was public first and when the report itself came out (url -> Release).

        Reads only the saved index; nothing is fetched or written.
        """
        index_path = self.archive_dir / symbol.upper() / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        out = {}
        for e in index:
            if e["kind"] != "report":
                continue
            express = next((x for x in index if x["kind"] == "express" and (x["year"], x["quarter"]) == (e["year"], e["quarter"])), None)
            if express is not None and pd.Timestamp(express["published"]) < pd.Timestamp(e["published"]):
                out[e["url"]] = Release(pd.Timestamp(express["published"]), "express report", express["url"], pd.Timestamp(e["published"]), pd.Timestamp(e["published"]))
        return out

    def _org_id(self, code: str) -> str:
        for item in self.http.post_json(f"{BASE}/new/information/topSearch/query", {"keyWord": code, "maxNum": 10}):
            if item["code"] == code:
                return item["orgId"]
        raise RuntimeError(f"CNINFO does not know the stock code {code}")

    def _listing(self, code: str, org_id: str, start: str, end: str, search: str = "", category: str = "") -> list[dict]:
        out, page = [], 1
        while True:
            data = self.http.post_json(
                f"{BASE}/new/hisAnnouncement/query",
                {
                    "stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": 30, "pageNum": page,
                    "column": exchange_column(code), "category": category, "plate": "", "seDate": f"{start}~{end}",
                    "searchkey": search, "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true",
                },
            )
            out += data.get("announcements") or []
            if not data.get("hasMore"):
                return out
            page += 1

    def _fetch_new(self, symbol: str, since: datetime, folder: Path, index: list[dict]) -> bool:
        code = symbol.split(".")[0]
        org_id = self._org_id(code)
        end = datetime.now().strftime("%Y-%m-%d")
        found = []
        for year in range(pd.Timestamp(since).year - 1, datetime.now().year + 1):  # one query per year keeps each result within one page
            found += self._listing(code, org_id, f"{year}-01-01", f"{year}-12-31", category=REPORT_CATEGORIES)
        found += self._listing(code, org_id, f"{pd.Timestamp(since).year - 1}-01-01", end, search=cn.EXPRESS_SEARCH_WORD)

        known = {str(e["id"]) for e in index}
        best: dict[tuple[str, int, int], dict] = {}
        for a in found:
            title = a["announcementTitle"].replace("<em>", "").replace("</em>", "").strip()
            cls = classify(title)
            if cls is None or str(a["announcementId"]) in known:
                continue
            key = (cls[0], cls[1], cls[2])
            # When a report has both a full-text and a body-text file, take the full text; report periods already saved are not saved again
            if any((e["kind"], e["year"], e["quarter"]) == key for e in index):
                continue
            rank = (listing_time(a["announcementTime"]), 0 if cn.BODY_TEXT_WORD not in title else 1)
            if key not in best or rank < best[key]["rank"]:
                best[key] = {"rank": rank, "title": title, "a": a}

        added = False
        for (kind, year, quarter), item in sorted(best.items(), key=lambda kv: kv[1]["rank"]):
            a, listed = item["a"], item["rank"][0]
            if listed + timedelta(days=1) < pd.Timestamp(since):
                continue
            url = STATIC + a["adjunctUrl"]
            resp = self.http.get_response(url)
            published, basis = published_time(listed, resp.headers.get("Last-Modified"))
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{published:%Y-%m-%d}_{a['announcementId']}.pdf"
            (folder / name).write_bytes(excerpt_pages(resp.content))
            index.append(
                {
                    "id": str(a["announcementId"]), "kind": kind, "year": year, "quarter": quarter, "title": english_title(kind, year, quarter),
                    "listed": f"{listed:%Y-%m-%dT%H:%M:%S}", "published": f"{published:%Y-%m-%dT%H:%M:%S}", "basis": basis,
                    "url": url, "file": name,
                }
            )
            added = True
            print(f"{symbol}: new filing {english_title(kind, year, quarter)}, published {published} ({basis})", file=sys.stderr)
        return added
