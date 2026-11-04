"""Save downloaded and extracted data in data/valuation.db (a SQLite file committed with the code) so the next run reuses it.

  data/valuation.db                     prices of each source, FX rates, final prices, parsed filings, per-period earnings and TTM data (see db.py)
  data/filings/<symbol>/                downloaded original filings (or relevant pages) and index.json (written by each source's downloader, see providers/)

Prices are downloaded only for trading days after the last saved one. Raw traded prices and FX rates are never changed once saved; Yahoo's
ex-rights adjusted prices change retroactively with new ex-rights events, and when a change is found the whole history is downloaded again. Today's prices can still change before the close, so they are not saved.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd

from valuation_monitor.db import FINAL_COLUMNS, Database
from valuation_monitor.filings import (
    PARSER_VERSIONS,
    EarningsHistory,
    adjust_for_splits,
    announcements_from_frame,
    announcements_to_frame,
    build_history,
    parse_announcement,
)
from valuation_monitor.market_calendar import HK, Market
from valuation_monitor.prices import consolidate
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.tencent import TencentQuoteProvider
from valuation_monitor.providers.yahoo import PriceHistory, YahooProvider

OVERLAP_DAYS = 7  # days of overlap with the saved data on incremental downloads, to check whether the source changed its history
MISMATCH_TOLERANCE = 1e-4  # relative difference


def split_adjust(close: pd.Series, splits: pd.Series) -> pd.Series:
    """Convert prices before a split to the current share basis (consistent with the split-adjusted EPS)."""
    out = close.copy()
    for day, ratio in splits.items():
        out[out.index < day] = out[out.index < day] / ratio
    return out


BAR_DECIMALS = {"open": 3, "high": 3, "low": 3, "close": 3, "volume": 0}
YAHOO_DECIMALS = {"open": 4, "high": 4, "low": 4, "close": 4, "adjclose": 4, "volume": 0}


class PriceStore:
    """Raw data of each source is saved separately, then the final closing price is decided:

    price_bars (source tencent)   Tencent daily bars (unadjusted actual traded price)
    price_bars (source yahoo)     Yahoo daily bars (close is ex-rights adjusted, adjclose further adjusted for dividends)
    meta prices/<symbol>/yahoo    includes Yahoo's split records and the currency
    prices_final                  combined result: price used each day, source, cross-check status
    """

    def __init__(self, quotes: TencentQuoteProvider, yahoo: YahooProvider, root: str | Path = "data", db: Database | None = None):
        self.quotes = quotes
        self.yahoo = yahoo
        self.root = Path(root)
        self.db = db or Database(self.root / "valuation.db")

    def final(self, symbol: str) -> pd.DataFrame | None:
        """The final prices of one symbol (FINAL_COLUMNS, indexed by date), or None if nothing is saved yet."""
        df = self.db.read_series("prices_final", {"symbol": symbol}, FINAL_COLUMNS)
        return None if df.empty else df

    def prices(self, symbol: str, start: datetime, market: Market = HK) -> PriceHistory:
        """The final closing price (converted to the current share basis for splits) and the split records.

        HK stocks have two sources, Tencent (actual traded price) and Yahoo, which are cross-checked; US and KR stocks use Yahoo only,
        whose closing price is already split-adjusted and is not adjusted again.
        """
        if market.cross_checks_prices:
            tencent = self._frame(
                "price_bars",
                {"symbol": symbol, "source": "tencent"},
                f"prices/{symbol}/tencent",
                source=f"Tencent quote API {symbol} (unadjusted)",
                fetch=lambda since: self.quotes.daily_bars(symbol, since),
                start=start,
                decimals=BAR_DECIMALS,
                frozen=True,
                timezone=market.timezone,
            )
        else:
            tencent = pd.DataFrame(columns=list(BAR_DECIMALS), dtype=float)

        found_splits = []
        found_currency = []

        def fetch_yahoo(since: datetime) -> pd.DataFrame:
            h = self.yahoo.price_history(symbol, since)
            found_splits.append(h.splits)
            found_currency.append(h.currency)
            return h.bars

        try:
            yahoo = self._frame(
                "price_bars",
                {"symbol": symbol, "source": "yahoo"},
                f"prices/{symbol}/yahoo",
                source=f"Yahoo Finance {symbol}",
                fetch=fetch_yahoo,
                start=start,
                decimals=YAHOO_DECIMALS,
                frozen=False,  # Yahoo's adjusted prices change retroactively with new ex-rights events; the whole history is re-downloaded when a change is found
                timezone=market.timezone,
            )
        except Exception as e:
            if not market.cross_checks_prices:
                raise
            print(f"Yahoo Finance {symbol}: unavailable ({e}); using Tencent data only", file=sys.stderr)
            yahoo = pd.DataFrame(columns=list(YAHOO_DECIMALS))

        yahoo_scope = f"prices/{symbol}/yahoo"
        yahoo_meta = self.db.read_meta(yahoo_scope)
        if found_currency and not market.cross_checks_prices:
            yahoo_meta["currency"] = found_currency[-1]
            self.db.write_meta(yahoo_scope, yahoo_meta)
        splits = {pd.Timestamp(d): r for d, r in yahoo_meta.get("splits", {}).items()}
        for found in found_splits:
            splits.update(found.to_dict())
        if found_splits:
            yahoo_meta["splits"] = {f"{d:%Y-%m-%d}": r for d, r in sorted(splits.items())}
            self.db.write_meta(yahoo_scope, yahoo_meta)
        splits = pd.Series(splits, dtype=float).sort_index()

        final = consolidate(tencent, yahoo)
        today = pd.Timestamp.now(tz=market.timezone).tz_localize(None).normalize()
        saved = final[final.index < today]
        if not self.db.readonly:  # a read-only database (the MCP server) only reads what the update job saved
            self.db.write_series("prices_final", {"symbol": symbol}, saved[FINAL_COLUMNS], prune=True)
        close = split_adjust(final["close"], splits) if market.cross_checks_prices else final["close"]
        currency = self.quotes.currency if market.cross_checks_prices else yahoo_meta["currency"]
        return PriceHistory(close[close.index >= pd.Timestamp(start).normalize()], splits, currency, market.timezone)

    def fx(self, from_ccy: str, to_ccy: str, start: datetime) -> pd.Series:
        """Daily series of how many to_ccy 1 unit of from_ccy is worth."""
        pair = f"{from_ccy}{to_ccy}"
        rate = self._frame(
            "fx_rates",
            {"pair": pair},
            f"fx/{pair}",
            source=f"Yahoo Finance {pair}=X",
            fetch=lambda since: self.yahoo.price_history(f"{pair}=X", since).close.to_frame("rate"),
            start=start,
            decimals={"rate": 6},
            frozen=True,
            timezone="Europe/London",  # Yahoo splits FX daily bars by London time
        )["rate"]
        return rate[rate.index >= pd.Timestamp(start).normalize()]

    def _frame(
        self,
        table: str,
        keys: dict[str, str],
        scope: str,
        source: str,
        fetch: Callable[[datetime], pd.DataFrame],
        start: datetime,
        decimals: dict[str, int],
        frozen: bool,
        timezone: str,
    ) -> pd.DataFrame:
        """Read the saved data, download and save only the new trading days. Returns all saved data plus today's prices.

        frozen=True: saved data is never changed (raw traded prices, FX rates); if the source changes history only a notice is printed.
        frozen=False: when the source adjusts history retroactively (e.g. Yahoo's ex-rights adjusted prices) the whole history is downloaded again.
        timezone: the timezone the source uses to split trading days, used to decide which days have closed and can be saved.
        """
        columns = list(decimals)
        check = "close" if "close" in decimals else columns[0]
        stored = self.db.read_series(table, keys, columns)
        meta = self.db.read_meta(scope)
        rewrite = False  # True when the saved rows are replaced as a whole
        start = pd.Timestamp(start).normalize()
        today = pd.Timestamp.now(tz=timezone).tz_localize(None).normalize()

        # fetched_from records the earliest date already downloaded; only earlier parts (e.g. a larger lookback) need to be fetched
        fetched_from = pd.Timestamp(meta["fetched_from"]) if "fetched_from" in meta and not stored.empty else None
        full = fetched_from is None or start < fetched_from
        fetch_from = min(start, fetched_from) if fetched_from is not None else start
        try:
            live = fetch((fetch_from if full else stored.index[-1] - pd.Timedelta(days=OVERLAP_DAYS)).to_pydatetime())
            completed = live[live.index < today][columns].round(decimals)
            overlap = completed.index.intersection(stored.index)
            changed = (completed.loc[overlap, check] / stored.loc[overlap, check] - 1).abs() > MISMATCH_TOLERANCE
            if changed.any() and frozen:
                print(f"{source}: {int(changed.sum())} days of history differ from the saved data; keeping the saved data", file=sys.stderr)
            elif changed.any():
                print(f"{source}: history was adjusted retroactively (probably a new ex-rights event); re-downloading all history", file=sys.stderr)
                live = fetch(fetch_from.to_pydatetime())
                completed = live[live.index < today][columns].round(decimals)
                stored = stored.iloc[0:0]
                rewrite = True
        except Exception as e:
            if stored.empty:
                raise
            print(f"{source}: cannot update ({e}); using saved data (up to {stored.index[-1]:%Y-%m-%d})", file=sys.stderr)
            return stored

        merged = pd.concat([stored, completed[~completed.index.isin(stored.index)]]).sort_index()
        for col, places in decimals.items():
            if places == 0:
                merged[col] = merged[col].round().astype("Int64")
        self.db.write_series(table, keys, merged[columns], replace=rewrite)
        meta["source"] = source
        meta["fetched_from"] = f"{fetch_from:%Y-%m-%d}"
        self.db.write_meta(scope, meta)

        result = pd.concat([merged.astype(float), live[live.index >= today][columns]])  # add today's live prices (not saved)
        return result


class EarningsStore:
    def __init__(self, root: str | Path = "data", db: Database | None = None):
        self.db = db or Database(Path(root) / "valuation.db")

    def periods(self, symbol: str) -> pd.DataFrame | None:
        """The saved per-period earnings of one symbol (as written by EarningsHistory.periods_frame)."""
        return self.db.read_frame("periods", symbol)

    def ttm(self, symbol: str) -> pd.DataFrame | None:
        """The saved per-share data of one symbol (as written by EarningsHistory.ttm_frame)."""
        return self.db.read_frame("ttm", symbol)

    def load(
        self,
        symbol: str,
        filings: list[Filing],
        splits: pd.Series,
        trading_days: pd.DatetimeIndex | None = None,
        fmt: str = "highlights",
        market: Market = HK,
    ) -> EarningsHistory:
        """Parse the filings and compute EPS. Already parsed filings read the saved result; only new filings are parsed.

        trading_days is used to list in the saved detail the trading day each value actually starts to be used; fmt is the layout of the filings.
        """
        version = PARSER_VERSIONS[fmt]
        saved = {}
        df = self.db.read_frame("announcements", symbol, text=True)
        if df is not None:
            df = df[df["parser_version"].astype(int) == version]
            saved = {a.url: a for a in announcements_from_frame(df, market)}

        parsed = []
        for f in filings:
            try:
                parsed.append(saved.get(f.url) or parse_announcement(f, fmt, market))
            except Exception as e:
                if not f.provisional:
                    raise
                print(f"{symbol}: cannot parse the provisional data {f.url} ({e}); waiting for the official report", file=sys.stderr)
        history = build_history(adjust_for_splits(parsed, splits), splits)

        if not self.db.readonly:  # the saved frames are a cache of what was just computed; a read-only database (the MCP server) keeps its copy as it is
            self.db.write_frame("announcements", symbol, announcements_to_frame(parsed, version), text=True)
            self.db.write_frame("periods", symbol, history.periods_frame(trading_days))
            self.db.write_frame("ttm", symbol, history.ttm_frame(trading_days), date_format="%Y-%m-%d")
        return history
