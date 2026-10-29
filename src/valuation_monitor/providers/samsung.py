"""三星电子 IR 网站（samsung.com/global/ir）：英文版合并财务报表和业绩发布会日程。

每个季度取两份原文 PDF：合并损益表（soi）和合并财务状况表（bs），保存在 data/filings/<股票代码>/。

财报 PDF 本身没有发布时间，而且英文版比首次公开业绩晚一个月左右上传，所以发布时间取 IR 网站“Events”页面
公布的该季度业绩发布会（Earnings Conference Call）的日期和时间（2009 年至今的每个季度都有记录，
如 “3Q26 Earnings Conference Call, October 29, 2026, 10:00 a.m. KST”）。这是市场第一次看到这些数字的时候。
2014 年及以前只写了日期，按 10:00 处理。发布会都在韩国交易所收盘（15:30）之前，所以生效交易日就是发布会当天。
注意：程序读取的是随后发布的、经会计师审阅的英文财务报表，其中的数字与发布会上的未审阅数字如有差异，以财报为准。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.http import HttpClient

BASE = "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/"
STATEMENT_PAGES = [
    "https://www.samsung.com/global/ir/financial-information/audited-financial-statements/",
    "https://www.samsung.com/global/ir/financial-information/earnings-release/",
]
EVENTS_PAGE = "https://www.samsung.com/global/ir/ir-events-presentations/events/"
STATEMENT_RE = re.compile(r"(?P<year>20\d\d)_con_quarter0(?P<q>[1-4])_(?P<kind>soi|bs)(?P<variant>_[a-z0-9]+)?\.pdf")
CALL_RE = re.compile(r"^(?P<q>[1-4])Q ?(?:FY)?(?P<year>\d{2}|\d{4}) Earnings Conference Call")
CALL_TIME_RE = re.compile(r"^(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})(?:, (?P<hour>\d{1,2}):(?P<minute>\d\d) a\.m\. KST)?")
DECK_RE = re.compile(r"(?P<year>20\d\d)_(?P<q>[1-4])Q_conference_eng\w*\.pdf")
DEFAULT_CALL_TIME = "10:00"


def parse_calls(html: str) -> dict[tuple[int, int], pd.Timestamp]:
    """Events 页面里各季度业绩发布会的时间（韩国时间）：{(财年, 季度): 时间}。"""
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
        if not pd.Timedelta(days=15) <= when - quarter_end <= pd.Timedelta(days=60):  # 页面上个别条目的年份写错
            continue
        calls[(year, quarter)] = when
    return calls


def discover(html_pages: list[str]) -> dict[tuple[int, int], dict[str, str]]:
    """从网页里找出每个季度的财务报表文件链接：{(年, 季度): {"soi": 链接, "bs": 链接}}。"""
    statements: dict[tuple[int, int], dict[str, str]] = {}
    for html in html_pages:
        for name in sorted(set(re.findall(r"[\w.-]+\.pdf", html))):
            if m := STATEMENT_RE.fullmatch(name):
                kinds = statements.setdefault((int(m.group("year")), int(m.group("q"))), {})
                # 同一份报表有多个版本（_new、_1）时用带后缀的，它们是更正后重新上传的
                if m.group("kind") not in kinds or m.group("variant"):
                    kinds[m.group("kind")] = BASE + name
    return statements


def discover_decks(html_pages: list[str]) -> dict[tuple[int, int], str]:
    """业绩发布会材料（presentation）的链接：{(年, 季度): 链接}。"""
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
        """返回发布会在 since 之后的全部季度业绩，按发布时间升序。"""
        folder = self.archive_dir / symbol.upper()
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        known = {(e["year"], e["quarter"]) for e in index if not e.get("provisional")}

        try:
            added = self._fetch_new(symbol, since, folder, index, known)
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: 无法访问三星 IR 网站（{e}），使用本地已保存的 {len(index)} 份财报", file=sys.stderr)
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
                filings.append(
                    Filing(
                        title=title,
                        published=pd.Timestamp(e["published"]),
                        url=e["url"],
                        content=(folder / e["files"]["soi"]).read_bytes(),
                        attachments={"bs": (folder / e["files"]["bs"]).read_bytes()},
                    )
                )
        return filings

    def _fetch_new(self, symbol, since, folder, index, known) -> bool:
        pages = [self.http.get_bytes(url).decode("utf-8", "replace") for url in STATEMENT_PAGES]
        statements = discover(pages)
        decks = discover_decks(pages)
        calls = parse_calls(self.http.get_bytes(EVENTS_PAGE).decode("utf-8", "replace"))
        added = False

        def too_old(year, quarter):
            quarter_end = pd.Timestamp(year=year, month=quarter * 3, day=1) + pd.offsets.MonthEnd(0)
            return quarter_end + pd.Timedelta(days=60) < pd.Timestamp(since)  # 发布会不可能在回看范围内

        for (year, quarter), kinds in sorted(statements.items()):
            if (year, quarter) in known or {"soi", "bs"} - kinds.keys() or too_old(year, quarter):
                continue
            published = calls.get((year, quarter))
            if published is None:
                print(f"{symbol}: {year}Q{quarter} 在 IR 网站的 Events 页面找不到业绩发布会日期，无法确定发布时间，跳过", file=sys.stderr)
                continue
            if published < pd.Timestamp(since):
                continue
            # 正式财报出来了：取代之前暂用的发布会材料数据
            for old in [e for e in index if (e["year"], e["quarter"]) == (year, quarter) and e.get("provisional")]:
                (folder / old["files"]["deck"]).unlink(missing_ok=True)
                index.remove(old)
            folder.mkdir(parents=True, exist_ok=True)
            files = {}
            for kind, url in kinds.items():
                files[kind] = f"{year}Q{quarter}_{url.rsplit('/', 1)[-1]}"
                (folder / files[kind]).write_bytes(self.http.get_bytes(url, cache=False))
            index.append({"year": year, "quarter": quarter, "published": f"{published:%Y-%m-%dT%H:%M:%S}", "url": kinds["soi"], "files": files})
            known.add((year, quarter))
            added = True
            print(f"{symbol}: 新财报 {year}Q{quarter}，业绩发布会 {published}", file=sys.stderr)

        # 还没有英文财报的最新季度：先用业绩发布会材料里的数据（发布会当天就有）
        have_provisional = {(e["year"], e["quarter"]) for e in index if e.get("provisional")}
        for (year, quarter), deck in sorted(decks.items()):
            published = calls.get((year, quarter))
            if (year, quarter) in known | have_provisional or published is None or published < pd.Timestamp(since) or too_old(year, quarter):
                continue
            if previous_quarter(year, quarter) not in known:
                continue  # 净资产估算要用上一季度的财报
            name = f"{year}Q{quarter}_{deck.rsplit('/', 1)[-1]}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(self.http.get_bytes(deck, cache=False))
            index.append(
                {"year": year, "quarter": quarter, "published": f"{published:%Y-%m-%dT%H:%M:%S}", "url": deck, "files": {"deck": name}, "provisional": True}
            )
            added = True
            print(f"{symbol}: {year}Q{quarter} 英文财报还没发布，先用业绩发布会材料（{published}）", file=sys.stderr)
        return added
