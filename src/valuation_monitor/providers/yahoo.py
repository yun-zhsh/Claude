"""Yahoo Finance: daily prices and split records; FX rates come from XXXYYY=X symbols.

Note that Yahoo's close is not the raw traded price: besides splits it also adjusts retroactively for distributions in kind etc.;
adjclose further adjusts for cash dividends. Both change as a whole after a new ex-rights event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from valuation_monitor.providers.http import HttpClient

CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"


@dataclass
class PriceHistory:
    close: pd.Series  # indexed by trading day (timezone-naive dates), values are closing prices
    splits: pd.Series  # indexed by split effective date, values are split ratios (5:1 is 5.0)
    currency: str
    timezone: str = "UTC"  # exchange timezone
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
            raise RuntimeError(f"Yahoo returned no quotes for {symbol}: {data.get('chart', {}).get('error')}")
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
