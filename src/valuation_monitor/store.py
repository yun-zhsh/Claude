"""Save downloaded and extracted data in data/ (committed with the code) so the next run reuses it.

  data/filings/<symbol>/                downloaded original filings (or relevant pages) and index.json (written by each source's downloader, see providers/)
  data/prices/<symbol>/                 raw daily data of each source, and the combined final closing price (see PriceStore)
  data/fx/<pair>.csv, <pair>.json       FX rates
  data/eps/<symbol>_announcements.csv   parse result of every earnings filing (numbers as in the filing)
  data/eps/<symbol>_periods.csv         earnings per period (quarter or half year; EPS adjusted for splits), with cross-check information
  data/eps/<symbol>_ttm.csv             the per-share data used for valuation after each filing, with the values of each period and the formulas (see audit.py)

Prices are downloaded only for trading days after the last saved one. Raw traded prices and FX rates are never changed once saved; Yahoo's
ex-rights adjusted prices change retroactively with new ex-rights events, and when a change is found the whole history is downloaded again. Today's prices can still change before the close, so they are not saved.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd

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


def write_if_changed(path: Path, text: str) -> None:
    """Do not write when the content is unchanged, so the file does not show up in git changes for nothing."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_meta(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_meta(path: Path, meta: dict) -> None:
    write_if_changed(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


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

    data/prices/<symbol>/tencent.csv   Tencent daily bars (unadjusted actual traded price)
    data/prices/<symbol>/yahoo.csv     Yahoo daily bars (close is ex-rights adjusted, adjclose further adjusted for dividends)
    data/prices/<symbol>/yahoo.json    includes Yahoo's split records
    data/prices/<symbol>/final.csv     combined result: price used each day, source, cross-check status
    """

    def __init__(self, quotes: TencentQuoteProvider, yahoo: YahooProvider, root: str | Path = "data"):
        self.quotes = quotes
        self.yahoo = yahoo
        self.root = Path(root)

    def final_path(self, symbol: str) -> Path:
        return self.root / "prices" / symbol / "final.csv"

    def prices(self, symbol: str, start: datetime, market: Market = HK) -> PriceHistory:
        """The final closing price (converted to the current share basis for splits) and the split records.

        HK stocks have two sources, Tencent (actual traded price) and Yahoo, which are cross-checked; US and KR stocks use Yahoo only,
        whose closing price is already split-adjusted and is not adjusted again.
        """
        folder = self.root / "prices" / symbol
        if market.cross_checks_prices:
            tencent = self._frame(
                folder / "tencent.csv",
                folder / "tencent.json",
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
                folder / "yahoo.csv",
                folder / "yahoo.json",
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

        yahoo_meta = read_meta(folder / "yahoo.json")
        if found_currency and not market.cross_checks_prices:
            yahoo_meta["currency"] = found_currency[-1]
            write_meta(folder / "yahoo.json", yahoo_meta)
        splits = {pd.Timestamp(d): r for d, r in yahoo_meta.get("splits", {}).items()}
        for found in found_splits:
            splits.update(found.to_dict())
        if found_splits:
            yahoo_meta["splits"] = {f"{d:%Y-%m-%d}": r for d, r in sorted(splits.items())}
            write_meta(folder / "yahoo.json", yahoo_meta)
        splits = pd.Series(splits, dtype=float).sort_index()

        final = consolidate(tencent, yahoo)
        today = pd.Timestamp.now(tz=market.timezone).tz_localize(None).normalize()
        saved = final[final.index < today]
        write_if_changed(self.final_path(symbol), saved.to_csv(index_label="date", date_format="%Y-%m-%d"))
        close = split_adjust(final["close"], splits) if market.cross_checks_prices else final["close"]
        currency = self.quotes.currency if market.cross_checks_prices else yahoo_meta["currency"]
        return PriceHistory(close[close.index >= pd.Timestamp(start).normalize()], splits, currency, market.timezone)

    def fx(self, from_ccy: str, to_ccy: str, start: datetime) -> pd.Series:
        """Daily series of how many to_ccy 1 unit of from_ccy is worth."""
        pair = f"{from_ccy}{to_ccy}"
        folder = self.root / "fx"
        rate = self._frame(
            folder / f"{pair}.csv",
            folder / f"{pair}.json",
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
        csv_path: Path,
        meta_path: Path,
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
        stored = pd.read_csv(csv_path, index_col="date", parse_dates=True) if csv_path.exists() else None
        if stored is None or set(columns) - set(stored.columns):  # no data or the format changed
            stored = pd.DataFrame(columns=columns, dtype=float)
        meta = read_meta(meta_path)
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
        except Exception as e:
            if stored.empty:
                raise
            print(f"{source}: cannot update ({e}); using saved data (up to {stored.index[-1]:%Y-%m-%d})", file=sys.stderr)
            return stored

        merged = pd.concat([stored, completed[~completed.index.isin(stored.index)]]).sort_index()
        for col, places in decimals.items():
            if places == 0:
                merged[col] = merged[col].round().astype("Int64")
        write_if_changed(csv_path, merged.to_csv(index_label="date", date_format="%Y-%m-%d"))
        meta["source"] = source
        meta["fetched_from"] = f"{fetch_from:%Y-%m-%d}"
        write_meta(meta_path, meta)

        result = pd.concat([merged.astype(float), live[live.index >= today][columns]])  # add today's live prices (not saved)
        return result


class EarningsStore:
    def __init__(self, root: str | Path = "data"):
        self.folder = Path(root) / "eps"

    def paths(self, symbol: str) -> dict[str, Path]:
        return {kind: self.folder / f"{symbol}_{kind}.csv" for kind in ("announcements", "periods", "ttm")}

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
        paths = self.paths(symbol)
        saved = {}
        if paths["announcements"].exists():
            df = pd.read_csv(paths["announcements"], dtype=str)
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

        write_if_changed(paths["announcements"], announcements_to_frame(parsed, version).to_csv(index=False))
        write_if_changed(paths["periods"], history.periods_frame(trading_days).to_csv(index=False))
        write_if_changed(paths["ttm"], history.ttm_frame(trading_days).to_csv(index=False, date_format="%Y-%m-%d"))
        return history
