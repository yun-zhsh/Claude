"""巨潮资讯网（cninfo.com.cn，中国证监会指定的上市公司信息披露网站）：A 股公司的定期报告和业绩快报原文 PDF。

下载的文件保存在 data/filings/<股票代码>/，目录中的 index.json 记录每份公告的标题、发布时间、原文链接。
为了控制仓库体积，只保存 PDF 里程序要用的页（主要财务数据表、合并利润表、合并资产负债表），页面原样保留。

发布时间：
  - 公告列表里只有日期（巨潮资讯把盘后发布的公告记在次日 00:00，如周五晚发布的显示为周六 00:00），
    所以把它当作“当天开盘前”处理：生效交易日就是该日或之后的第一个交易日，与实际发布时间的判断一致。
  - 2020 年 10 月起，PDF 文件的上传时间（HTTP Last-Modified）就是实际发布时间（海康威视一般在周五 16:00～22:00，
    收盘后），有这个时间的直接采用。更早的文件在 2020 年 9 月整体迁移过，上传时间已失真，不能用。

业绩快报：年度业绩快报在年报之前（1～2 月）发布，是第一次公布全年业绩。年报发布后，程序用年报的精确数字
取代快报数字，但发布时间仍取快报的（市场是在快报那天第一次看到全年业绩的）；快报和年报的数字如有差异以年报为准。
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

from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.http import HttpClient

BASE = "https://www.cninfo.com.cn"
STATIC = "https://static.cninfo.com.cn/"
CST = timezone(timedelta(hours=8))
REPORT_CATEGORIES = "category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh;"
TRUSTED_UPLOAD_TIME_FROM = pd.Timestamp("2020-10-01")  # 此前的文件在 2020 年 9 月迁移过，上传时间不是发布时间
REQUEST_INTERVAL = 0.5
EXCLUDED = ("摘要", "英文", "ESG", "社会责任", "内部控制", "审计", "环境", "更正", "取消", "补充", "说明", "预告")
TITLE_RE = re.compile(r"^(?P<year>\d{4})年(?P<kind>第一季度|一季度|半年度|第三季度|三季度|年度)报告(?:全文|正文)?(?:（更新后）)?$")
EXPRESS_RE = re.compile(r"^(?P<year>\d{4})年(?P<kind>半年度|度)业绩快报$")
QUARTER_OF = {"第一季度": 1, "一季度": 1, "半年度": 2, "第三季度": 3, "三季度": 3, "年度": 4, "度": 4}


def classify(title: str) -> tuple[str, int, int] | None:
    """公告标题 -> （类型 report/express, 年, 季度）；不是我们要的公告返回 None。"""
    title = title.replace("<em>", "").replace("</em>", "").strip()
    if any(word in title for word in EXCLUDED):
        return None
    if m := TITLE_RE.match(title):
        return "report", int(m.group("year")), QUARTER_OF[m.group("kind")]
    if m := EXPRESS_RE.match(title):
        return "express", int(m.group("year")), QUARTER_OF[m.group("kind")]
    return None


def exchange_column(code: str) -> str:
    return "sse" if code.startswith("6") else "szse"


def listing_time(ms: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.fromtimestamp(ms / 1000, CST).replace(tzinfo=None))


def published_time(listed: pd.Timestamp, last_modified: str | None) -> tuple[pd.Timestamp, str]:
    """发布时间和判断依据。"""
    if last_modified:
        uploaded = pd.Timestamp(parsedate_to_datetime(last_modified).astimezone(CST).replace(tzinfo=None)).floor("s")
        # 上传时间应当在公告列表日期的前一天（盘后发布记在次日）或当天
        if uploaded >= TRUSTED_UPLOAD_TIME_FROM and timedelta(0) <= listed - uploaded.normalize() <= timedelta(days=1):
            return uploaded, "PDF 上传时间"
    return listed, "巨潮资讯公告日期（只有日期，盘后发布的记在次日 00:00）"


def excerpt_pages(pdf: bytes) -> bytes:
    """只保留程序要用的页：主要财务数据表、合并利润表、合并资产负债表（含紧接着的下一页），页面原样保留。"""
    reader = PdfReader(io.BytesIO(pdf))
    if len(reader.pages) <= 6:
        return pdf  # 业绩快报只有两页，原文整份保存
    texts = [" ".join((p.extract_text() or "").split()) for p in reader.pages]
    wanted: set[int] = set()
    for i, t in enumerate(texts):
        key_table = "主要会计数据和财务指标" in t and "营业收入" in t
        income = re.search(r"一、营业(?:总)?收入", t)
        balance = ("归属于母公司" in t and "权益合计" in t) or (re.search(r"(?<!总)股本", t) and "资本公积" in t)
        if key_table or income or balance:
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
        """返回 since 之后发布的定期报告和（年报还没出的）业绩快报，按发布时间升序。"""
        folder = self.archive_dir / symbol.upper()
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        try:
            added = self._fetch_new(symbol, since, folder, index)
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: 无法访问巨潮资讯（{e}），使用本地已保存的 {len(index)} 份公告", file=sys.stderr)
            added = False
        if added:
            index.sort(key=lambda e: (e["published"], e["id"]))
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        reports = {(e["year"], e["quarter"]): e for e in index if e["kind"] == "report"}
        since_ts = pd.Timestamp(since)
        filings = []
        for e in index:
            key = (e["year"], e["quarter"])
            if e["kind"] == "express" and key in reports:
                continue  # 正式报告已发布，快报被取代
            published, note = pd.Timestamp(e["published"]), ""
            if e["kind"] == "report":
                express = next((x for x in index if x["kind"] == "express" and (x["year"], x["quarter"]) == key), None)
                if express is not None and pd.Timestamp(express["published"]) < published:
                    # 数字第一次公开是在快报那天；年报的精确数字取代快报数字，发布时间沿用快报的
                    note = f"数字取自{e['title']}（{published:%Y-%m-%d %H:%M} 发布），发布时间沿用业绩快报（{pd.Timestamp(express['published']):%Y-%m-%d %H:%M}）；快报数字略有不同时以年报为准"
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

    def _org_id(self, code: str) -> str:
        for item in self.http.post_json(f"{BASE}/new/information/topSearch/query", {"keyWord": code, "maxNum": 10}):
            if item["code"] == code:
                return item["orgId"]
        raise RuntimeError(f"巨潮资讯找不到股票代码 {code}")

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
        for year in range(pd.Timestamp(since).year - 1, datetime.now().year + 1):  # 一年一查，避免结果超过一页
            found += self._listing(code, org_id, f"{year}-01-01", f"{year}-12-31", category=REPORT_CATEGORIES)
        found += self._listing(code, org_id, f"{pd.Timestamp(since).year - 1}-01-01", end, search="业绩快报")

        known = {str(e["id"]) for e in index}
        best: dict[tuple[str, int, int], dict] = {}
        for a in found:
            title = a["announcementTitle"].replace("<em>", "").replace("</em>", "").strip()
            cls = classify(title)
            if cls is None or str(a["announcementId"]) in known:
                continue
            key = (cls[0], cls[1], cls[2])
            # 同一份报告有全文、正文两个文件时取全文；已经保存过的报告期不再重复保存
            if any((e["kind"], e["year"], e["quarter"]) == key for e in index):
                continue
            rank = (listing_time(a["announcementTime"]), 0 if "正文" not in title else 1)
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
                    "id": str(a["announcementId"]), "kind": kind, "year": year, "quarter": quarter, "title": item["title"],
                    "listed": f"{listed:%Y-%m-%dT%H:%M:%S}", "published": f"{published:%Y-%m-%dT%H:%M:%S}", "basis": basis,
                    "url": url, "file": name,
                }
            )
            added = True
            print(f"{symbol}: 新公告 {item['title']}，发布时间 {published}（{basis}）", file=sys.stderr)
        return added
