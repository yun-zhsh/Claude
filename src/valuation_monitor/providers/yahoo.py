"""Yahoo Finance：日线行情、拆股记录；汇率用 XXXYYY=X 代码获取。

注意 Yahoo 的 close 不是原始成交价：除拆股外，它还会把实物分派等当作除权回溯调整；
adjclose 在此基础上再按现金分红调整。两者在发生新的除权事件后都会整体改变。
"""

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
    bars: pd.DataFrame | None = None  # open, high, low, close, adjclose, volume


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

        quote = r["indicators"]["quote"][0] if r.get("timestamp") else {}
        columns = {c: quote.get(c, []) for c in ("open", "high", "low", "close")}
        columns["adjclose"] = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose", columns["close"])
        columns["volume"] = quote.get("volume", [])
        bars = pd.DataFrame(columns, index=to_dates(r.get("timestamp", [])), dtype=float).dropna(subset=["close"])
        bars = bars[~bars.index.duplicated(keep="last")].sort_index()
        close = bars["close"]

        raw_splits = (r.get("events") or {}).get("splits") or {}
        splits = pd.Series(
            [s["numerator"] / s["denominator"] for s in raw_splits.values()],
            index=to_dates([s["date"] for s in raw_splits.values()]),
            dtype=float,
        ).sort_index()

        return PriceHistory(close=close, splits=splits, currency=r["meta"]["currency"], timezone=tz, bars=bars)
