"""港交所披露易（HKEXnews）：检索并下载上市公司的业绩公告原文 PDF。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from valuation_monitor.providers.http import HttpClient

BASE_URL = "https://www1.hkexnews.hk"
RESULTS_TITLE_RE = re.compile(r"^ANNOUNCEMENT OF THE (?:ANNUAL )?RESULTS FOR THE ", re.I)


@dataclass
class Filing:
    title: str
    published: pd.Timestamp  # 香港时间
    url: str
    pdf: bytes


def stock_code(symbol: str) -> str:
    """0700.HK -> 00700。"""
    code, _, market = symbol.upper().partition(".")
    if market != "HK":
        raise ValueError(f"暂只支持港股，收到 {symbol}")
    return code.zfill(5)


class HkexProvider:
    def __init__(self, http: HttpClient):
        self.http = http

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
        raise RuntimeError(f"披露易找不到股票代码 {code}")

    def results_announcements(self, symbol: str, since: datetime) -> list[Filing]:
        """返回 since 之后发布的全部业绩公告（英文版），按发布时间升序。"""
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
            raise RuntimeError("披露易检索结果超过一页，需要缩小时间范围")

        filings = []
        for row in json.loads(data["result"]):
            title = " ".join(row["TITLE"].split())
            if not RESULTS_TITLE_RE.match(title):
                continue
            url = BASE_URL + row["FILE_LINK"]
            filings.append(
                Filing(
                    title=title,
                    published=pd.Timestamp(datetime.strptime(row["DATE_TIME"], "%d/%m/%Y %H:%M")),
                    url=url,
                    pdf=self.http.get_bytes(url, immutable=True),
                )
            )
        return sorted(filings, key=lambda f: f.published)
