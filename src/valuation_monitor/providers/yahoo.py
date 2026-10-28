"""Yahoo Finance：日线收盘价（已做拆股复权、未做分红复权）和拆股记录；汇率用 XXXYYY=X 代码获取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from valuation_monitor.providers.http import HttpClient

CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"


@dataclass
class PriceHistory:
    close: pd.Series  # 索引为交易日（无时区的日期），值为收盘价
    splits: pd.Series  # 索引为拆股生效日，值为拆股比例（如 5:1 记为 5.0）
    currency: str
    timezone: str = "UTC"  # 交易所时区


class YahooProvider:
    def __init__(self, http: HttpClient):
        self.http = http

    def price_history(self, symbol: str, start: datetime) -> PriceHistory:
        data = self.http.get_json(
            CHART_URL.format(symbol=symbol),
            params={
                "period1": int(start.timestamp()),
                "period2": int(datetime.now().timestamp()) // 86400 * 86400 + 86400,
                "interval": "1d",
                "events": "split",
            },
        )
        result = (data.get("chart") or {}).get("result")
        if not result:
            raise RuntimeError(f"Yahoo 没有返回 {symbol} 的行情: {data.get('chart', {}).get('error')}")
        r = result[0]
        tz = r["meta"].get("exchangeTimezoneName", "UTC")

        def to_dates(ts) -> pd.DatetimeIndex:
            return pd.to_datetime(ts, unit="s", utc=True).tz_convert(tz).tz_localize(None).normalize()

        close = pd.Series(r["indicators"]["quote"][0]["close"], index=to_dates(r.get("timestamp", [])), dtype=float)
        close = close.dropna()
        close = close[~close.index.duplicated(keep="last")].sort_index()

        raw_splits = (r.get("events") or {}).get("splits") or {}
        splits = pd.Series(
            [s["numerator"] / s["denominator"] for s in raw_splits.values()],
            index=to_dates([s["date"] for s in raw_splits.values()]),
            dtype=float,
        ).sort_index()

        return PriceHistory(close=close, splits=splits, currency=r["meta"]["currency"], timezone=tz)
