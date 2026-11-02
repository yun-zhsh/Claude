"""Tencent Securities quote API: HK daily closing prices (unadjusted, i.e. the actual traded price of the day).

Yahoo's close is not used because Yahoo treats distributions in kind (e.g. Tencent distributing JD and Meituan shares)
as ex-rights events and retroactively lowers historical prices, while a historical PE needs the actual price at the time;
Yahoo's close on half days (Christmas Eve, Lunar New Year's Eve, ...) is also wrong.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from valuation_monitor.providers.hkex import stock_code
from valuation_monitor.providers.http import HttpClient

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
MAX_ROWS = 400  # row limit per request (the API returns at most about 640 rows); requests are split by calendar year


class TencentQuoteProvider:
    currency = "HKD"
    timezone = "Asia/Hong_Kong"

    def __init__(self, http: HttpClient):
        self.http = http

    def daily_bars(self, symbol: str, start: datetime) -> pd.DataFrame:
        """Daily bars from start to today (open, high, low, close, volume), indexed by trading day."""
        code = f"hk{stock_code(symbol)}"
        parts = []
        for year in range(pd.Timestamp(start).year, datetime.now().year + 1):
            begin = max(pd.Timestamp(start), pd.Timestamp(year, 1, 1))
            data = self.http.get_json(
                KLINE_URL, params={"param": f"{code},day,{begin:%Y-%m-%d},{year}-12-31,{MAX_ROWS},"}
            )
            if data.get("code") != 0 or not isinstance(data.get("data"), dict):
                raise RuntimeError(f"The Tencent quote API returned an error: {data.get('msg')}")
            rows = data["data"][code].get("day", [])
            # each row: date, open, close, high, low, volume (ex-rights info may follow)
            parts.append(
                pd.DataFrame(
                    [[float(x) for x in (r[1], r[3], r[4], r[2], r[5])] for r in rows],
                    index=pd.to_datetime([r[0] for r in rows]),
                    columns=["open", "high", "low", "close", "volume"],
                )
            )
        bars = pd.concat(parts)
        return bars[~bars.index.duplicated(keep="last")].sort_index()
