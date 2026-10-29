"""腾讯证券行情接口：港股日线收盘价（未复权，即当天实际成交价）。

不用 Yahoo 的收盘价，是因为 Yahoo 会把实物分派（如腾讯分派京东、美团股份）
当作除权回溯调低历史价格，而算历史 PE 需要的是当时的实际价格；Yahoo 在半日市
（平安夜、除夕等）的收盘价也有误。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from valuation_monitor.providers.hkex import stock_code
from valuation_monitor.providers.http import HttpClient

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
MAX_ROWS = 400  # 单次请求的行数上限（接口最多约 640 行），按自然年分段请求


class TencentQuoteProvider:
    currency = "HKD"
    timezone = "Asia/Hong_Kong"

    def __init__(self, http: HttpClient):
        self.http = http

    def daily_bars(self, symbol: str, start: datetime) -> pd.DataFrame:
        """start 至今的日线（open, high, low, close, volume），索引为交易日。"""
        code = f"hk{stock_code(symbol)}"
        parts = []
        for year in range(pd.Timestamp(start).year, datetime.now().year + 1):
            begin = max(pd.Timestamp(start), pd.Timestamp(year, 1, 1))
            data = self.http.get_json(
                KLINE_URL, params={"param": f"{code},day,{begin:%Y-%m-%d},{year}-12-31,{MAX_ROWS},"}
            )
            if data.get("code") != 0 or not isinstance(data.get("data"), dict):
                raise RuntimeError(f"腾讯行情接口返回错误: {data.get('msg')}")
            rows = data["data"][code].get("day", [])
            # 每行依次为：日期、开盘、收盘、最高、最低、成交量（之后可能还有除权信息等）
            parts.append(
                pd.DataFrame(
                    [[float(x) for x in (r[1], r[3], r[4], r[2], r[5])] for r in rows],
                    index=pd.to_datetime([r[0] for r in rows]),
                    columns=["open", "high", "low", "close", "volume"],
                )
            )
        bars = pd.concat(parts)
        return bars[~bars.index.duplicated(keep="last")].sort_index()
