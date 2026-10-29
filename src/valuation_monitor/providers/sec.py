"""美国证监会 EDGAR：检索并下载美股上市公司（外国私人发行人）的季度业绩新闻稿原文。

外国私人发行人不提交 10-Q，业绩以 6-K 的形式提交，正文是附件 99.1 的新闻稿（HTML）。
流程：从 submissions 接口列出全部 6-K → 看每份 6-K 的文件清单，挑出体量合适的新闻稿附件 →
标题是“… Announces … Results”的才是业绩公告。

下载过的业绩公告保存在 data/filings/<股票代码>/ 目录（随代码一起提交到仓库），目录中的 index.json
记录每份已检查过的 6-K（包括不是业绩公告的），下次不再重复检查。无法访问 EDGAR 时直接使用本地已保存的公告。

EDGAR 要求每个请求带上能联系到使用者的 User-Agent，并且每秒不超过 10 个请求。
联系方式不写在代码里，而是通过环境变量 SEC_USER_AGENT 提供，如“valuation-monitor 你的邮箱”。
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
REQUEST_INTERVAL = 0.2  # 秒；官方上限是每秒 10 个请求
# 业绩新闻稿附件的体量：太小的是通知，太大的是年报或招股文件之类（有的 6-K 附件超过 15MB）
MIN_EXHIBIT_BYTES = 100_000
MAX_EXHIBIT_BYTES = 3_000_000
EXHIBIT_RE = re.compile(r"ex-?99", re.I)
# 新闻稿开头的标题，如“JD.com Announces Second Quarter 2026 Results”“… Announces Fourth Quarter and Full Year 2025 Results”
EXHIBIT_LABEL_RE = re.compile(r"^.*?\bExhibi?t? ?99\.\d\s+", re.I)
RESULTS_HEADLINE_RE = re.compile(r"\bannounces?\b.{0,80}?\bresults\b", re.I)


class SecUserAgentMissing(RuntimeError):
    pass


def make_http_client(cache_dir: str | Path = ".cache", ttl_seconds: int = 12 * 3600, refresh: bool = False) -> HttpClient:
    agent = os.environ.get(USER_AGENT_ENV, "").strip()
    if not agent:
        raise SecUserAgentMissing(
            f"访问美国证监会需要声明使用者：请设置环境变量 {USER_AGENT_ENV}，内容为“程序名 联系邮箱”，例如 valuation-monitor you@example.com"
        )
    return HttpClient(cache_dir, ttl_seconds, refresh, user_agent=agent, min_interval=REQUEST_INTERVAL)


def to_new_york(accepted: str) -> pd.Timestamp:
    """acceptanceDateTime（UTC，如 2026-08-13T10:05:29.000Z）-> 纽约当地时间（不带时区）。"""
    return pd.Timestamp(accepted).tz_convert("America/New_York").tz_localize(None).floor("s")


def headline(html: bytes) -> str | None:
    """新闻稿开头的标题；不是业绩新闻稿时返回 None。"""
    from bs4 import BeautifulSoup

    text = " ".join(BeautifulSoup(html[:200_000], "lxml").get_text(" ").split())
    text = EXHIBIT_LABEL_RE.sub("", text, count=1)[:1500]  # 去掉文件头里的“EX-99.1 Exhibit 99.1”之类的字样
    m = RESULTS_HEADLINE_RE.search(text)
    if not m:
        return None
    return text[: m.end()].strip() if m.start() <= 60 else text[m.start() - 40 : m.end()].strip()


class SecProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def results_announcements(self, symbol: str, cik: int, since: datetime) -> list[Filing]:
        """返回 since 之后发布的全部业绩新闻稿，按发布时间升序。"""
        folder = self.archive_dir / symbol.upper()
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        known = {e["accession"] for e in index}

        try:
            if self.http is None:
                raise SecUserAgentMissing(f"没有设置环境变量 {USER_AGENT_ENV}")
            added = self._examine_new(symbol, cik, since, folder, index, known)
        except Exception as e:
            if not any(e_["results"] for e_ in index):
                raise
            print(f"{symbol}: 无法访问美国证监会（{e}），使用本地已保存的 {sum(x['results'] for x in index)} 份公告", file=sys.stderr)
            added = True  # 已检查完的部分照常写入索引

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
        for extra in data["filings"].get("files", []):  # 更早的记录分在其他文件里
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
                print(f"{symbol}: 新业绩公告 {entry['published']} {entry['title']}", file=sys.stderr)
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
