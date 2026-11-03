"""A Model Context Protocol (MCP) server that lets an AI assistant look up valuations, with the safeguards that make the answers checkable.

Four rules, each enforced in code and covered by tests:

  read-only     the database is opened in SQLite's read-only mode and the program never goes online while serving
                (Monitor(offline=True, readonly=True)); a tool call cannot change or fetch anything
  sourced       every number comes with its origin: the original filing (link), publication time, the trading day it
                took effect, and the calculation written out by audit.py, the same text as the web tooltips
  point in time every tool takes an `as_of` date and only uses data that was already published and effective by then
                (the project's "no future data" rule, extended to the assistant)
  no model math all calculation (ratios, percentiles, levels) happens here in Python through the same code as the web page;
                the assistant only chooses which tool to call and explains the result

The tools return plain dictionaries (ValuationService); `create_server` wraps them for the MCP library, `main` runs it over stdio:

  valuation-monitor-mcp --data-dir data
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from valuation_monitor.cloud import RemoteDatabase
from valuation_monitor.analysis import BAND_PERCENTILES, evaluate
from valuation_monitor.audit import explain, num
from valuation_monitor.config import Config, StockConfig, load_config
from valuation_monitor.data import StockData
from valuation_monitor.filings import effective_day, period_label
from valuation_monitor.market_calendar import get_market
from valuation_monitor.metrics import METRICS
from valuation_monitor.service import Monitor

MAX_HISTORY_POINTS = 240
COVERAGE_TOLERANCE_DAYS = 10  # the first usable day may be a few days after the start of the window (weekends, holidays)

# which saved per-period series feed each per-share column of the metric calculations (see audit.py)
SOURCE_SERIES = {
    "eps_ttm": [("eps", "Basic EPS")],
    "non_ifrs_eps_ttm": [("non_ifrs_eps", "Non-IFRS/Non-GAAP EPS")],
    "revenue_per_share_ttm": [("revenue", "Revenue")],
    "book_value_per_share": [],
}

UNITS = {  # of the saved per-period series, in the reporting currency of the filing
    "eps": "reporting currency per share",
    "non_ifrs_eps": "reporting currency per share",
    "revenue": "millions of reporting currency",
    "profit": "millions of reporting currency",
    "equity": "millions of reporting currency",
    "wa_shares": "millions of shares (ADS for ADRs)",
}

RULES = (
    "All numbers were calculated by this server in Python from the saved original filings and prices. "
    "Quote them as given; do not recalculate, round differently or extrapolate. "
    "Only data published on or before the as_of date was used."
)


class RequestError(ValueError):
    """A request that cannot be answered; the message says why and what can be asked instead."""


def _day(value: pd.Timestamp | None) -> str | None:
    return None if value is None or pd.isna(value) else f"{pd.Timestamp(value):%Y-%m-%d}"


def _float(value: Any, digits: int = 4) -> float | None:
    return None if value is None or pd.isna(value) else round(float(value), digits)


def parse_date(text: str | None, name: str) -> pd.Timestamp | None:
    if text is None or str(text).strip() == "":
        return None
    try:
        return pd.Timestamp(date.fromisoformat(str(text).strip()))
    except ValueError:
        raise RequestError(f"{name} must be a date written YYYY-MM-DD, got {text!r}") from None


@dataclass
class Loaded:
    data: StockData
    details: dict[str, pd.DataFrame]  # metric key -> the metric's daily calculation (PriceMultiple.detail)
    final: pd.DataFrame | None  # prices_final: source and cross-check status of each day


class ValuationService:
    """The tools. `today` is only used to tell how old the newest price is; it never decides which data is used."""

    def __init__(
        self,
        monitor: Monitor,
        today: date | None = None,
        sync: Callable[[], object] | None = None,
        sync_every: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`sync` brings the local database up to date (on Cloud Run: download the newest bucket object); it runs at most every `sync_every` seconds."""
        if not monitor.db.readonly:
            raise ValueError("The MCP server only works on a read-only database")
        self.monitor = monitor
        self.config: Config = monitor.config
        self._today = today
        self._sync = sync
        self._sync_every = sync_every
        self._clock = clock
        self._synced_at: float | None = None
        self._cache: dict[str, tuple[float, Loaded]] = {}

    # ---- loading (always from the saved data) ---------------------------------------------------------------------------------

    def _stock(self, symbol: str) -> StockConfig:
        for stock in self.config.stocks:
            if stock.symbol.lower() == str(symbol).strip().lower():
                return stock
        known = ", ".join(s.symbol for s in self.config.stocks)
        raise RequestError(f"Unknown symbol {symbol!r}. Available: {known}")

    def _maybe_sync(self) -> None:
        if self._sync is None:
            return
        now = self._clock()
        if self._synced_at is not None and now - self._synced_at < self._sync_every:
            return
        self._synced_at = now
        try:
            self._sync()
        except Exception as e:  # keep answering from the copy already on disk
            print(f"Could not check for a newer database: {e}", file=sys.stderr)

    def _load(self, stock: StockConfig) -> Loaded:
        self._maybe_sync()
        mtime = self.monitor.db.path.stat().st_mtime  # the file is replaced when the update job uploads a new version
        cached = self._cache.get(stock.symbol)
        if cached and cached[0] == mtime:
            return cached[1]
        with contextlib.redirect_stdout(sys.stderr), contextlib.redirect_stderr(io.StringIO()):  # stdout is the protocol channel
            data = self.monitor.loader.load(stock)
            details = {key: METRICS[key].detail(data) for key in stock.metrics}
            final = self.monitor.prices.final(stock.symbol)
        loaded = Loaded(data, details, final)
        self._cache[stock.symbol] = (mtime, loaded)
        return loaded

    def _metric(self, stock: StockConfig, key: str):
        key = str(key).strip().lower()
        if key not in stock.metrics:
            raise RequestError(f"{stock.symbol} has no metric {key!r}. Available: {', '.join(stock.metrics)}")
        return METRICS[key]

    def _years(self, years: int | None) -> int:
        years = self.config.lookback_years if years is None else int(years)
        if not 1 <= years <= self.config.max_lookback:
            raise RequestError(f"years must be between 1 and {self.config.max_lookback} (the data prepared for the lookback)")
        return years

    def _age(self, day: pd.Timestamp) -> int:
        return ((self._today or date.today()) - day.date()).days

    # ---- tools ----------------------------------------------------------------------------------------------------------------

    def list_stocks(self) -> dict:
        """The stocks that can be asked about, their metrics, and how far the saved data reaches."""
        stocks = []
        for stock in self.config.stocks:
            entry: dict[str, Any] = {
                "symbol": stock.symbol,
                "name": stock.name,
                "market": stock.market,
                "metrics": {key: METRICS[key].label for key in stock.metrics},
            }
            try:
                loaded = self._load(stock)
                close = loaded.data.close
                earnings = loaded.data.earnings
                entry.update(
                    price_currency=loaded.data.price_currency,
                    first_price_date=_day(close.index[0]),
                    latest_price_date=_day(close.index[-1]),
                    latest_price_age_days=self._age(close.index[-1]),
                    filings_saved=len(earnings.announcements) if earnings else 0,
                    latest_filing_published=_day(max((a.published for a in earnings.announcements), default=None)) if earnings else None,
                )
            except Exception as e:  # one stock failing to load must not hide the others
                entry["error"] = f"Saved data could not be read: {e}"
            stocks.append(entry)
        return {
            "stocks": stocks,
            "lookback_years": {"default": self.config.lookback_years, "allowed": list(range(1, self.config.max_lookback + 1))},
            "levels": [{"label": lv.label, "percentile_up_to": lv.upper} for lv in self.config.levels],
            "note": RULES,
        }

    def valuation(self, symbol: str, metric: str, years: int | None = None, as_of: str | None = None) -> dict:
        """Where a metric stands in its own history on the as_of day (default: the newest saved day), with the sources and the calculation."""
        stock = self._stock(symbol)
        m = self._metric(stock, metric)
        years = self._years(years)
        asked = parse_date(as_of, "as_of")
        loaded = self._load(stock)
        data, detail = loaded.data, loaded.details[m.key]
        warnings: list[str] = []

        newest = detail.index[-1]
        if asked is None:
            cutoff = newest
        elif asked > newest:
            cutoff = newest
            warnings.append(f"as_of {_day(asked)} is after the newest saved price ({_day(newest)}); the newest saved day was used")
        else:
            cutoff = asked
        seen = detail[detail.index <= cutoff]  # nothing after the cutoff is looked at from here on
        series = seen["ratio"].dropna()
        if series.empty:
            raise RequestError(
                f"No {m.label} value for {stock.symbol} on or before {_day(cutoff)}: no earnings were published yet by then, or the company had a loss / negative equity. "
                f"The first saved price is {_day(detail.index[0])}."
            )
        valuation = evaluate(series, years, self.config.levels)
        value_day = valuation.as_of
        if (cutoff - value_day).days > 7:
            warnings.append(f"The latest usable value is from {_day(value_day)}, {(cutoff - value_day).days} days before {_day(cutoff)} (loss period or missing data)")
        wanted_start = value_day - pd.DateOffset(years=years)
        short = (valuation.window_start - wanted_start).days > COVERAGE_TOLERANCE_DAYS
        if short:
            warnings.append(
                f"Only {(value_day - valuation.window_start).days / 365.25:.1f} years of history exist before {_day(value_day)}, not the {years} asked for; "
                f"the percentile is within the shorter window starting {_day(valuation.window_start)}"
            )
        if asked is None and self._age(newest) > 5:
            warnings.append(f"The newest saved price is {self._age(newest)} days old")

        row = seen.loc[value_day]
        record_index = int(row["record"])
        earnings = data.earnings
        rec = earnings.ttm_detail[record_index]
        fundamentals = data.fundamentals.iloc[record_index]
        if rec.get("provisional"):
            warnings.append("The latest earnings used are preliminary (earnings call presentation or express report); they are replaced when the official report is out")
        close_row = loaded.final.loc[value_day] if loaded.final is not None and value_day in loaded.final.index else None
        per_share, fx = float(row["per_share"]), float(row["fx"])
        market = get_market(stock.market)

        return {
            "symbol": stock.symbol,
            "name": stock.name,
            "metric": {"key": m.key, "label": m.label},
            "as_of": _day(cutoff),
            "value_day": _day(value_day),
            "value": _float(valuation.current),
            "percentile": _float(valuation.percentile, 1),
            "level": valuation.level,
            "how_to_read": (
                f"The share of the {valuation.observations} trading days in the window with a value at or below today's. "
                f"Levels: " + "; ".join(f"up to {lv.upper}: {lv.label}" for lv in self.config.levels)
            ),
            "window": {
                "years_asked": years,
                "start": _day(valuation.window_start),
                "end": _day(value_day),
                "trading_days": valuation.observations,
                "covers_years_asked": not short,
            },
            "history_statistics": {
                "mean": _float(valuation.mean),
                "min": _float(valuation.min),
                "max": _float(valuation.max),
                "at_percentile": {str(p): _float(v) for p, v in valuation.bands.items()},
            },
            "calculation": {
                "ratio": f"{m.label} = close / (per-share value x FX rate) = "
                f"{num(float(row['close']))} {data.price_currency} / ({num(per_share)} {earnings.currency} x {num(fx)}) = {num(float(row['ratio']))}",
                "per_share_value": explain(m.column, rec),
            },
            "sources": {
                "earnings_record": {
                    "period": rec["period_label"],
                    "published": rec["published"],
                    "published_timezone": market.timezone,
                    "effective_trading_day": _day(effective_day(fundamentals["available_date"], data.close.index)),
                    "filing_url": self._filing_url(earnings, rec),
                    "preliminary": bool(rec.get("provisional")),
                },
                "components": self._components(earnings, rec, m.column, cutoff),
                "price": {
                    "day": _day(value_day),
                    "close": _float(row["close"], 3),
                    "currency": data.price_currency,
                    "source": None if close_row is None else str(close_row["source"]),
                    "cross_check": None if close_row is None else str(close_row["status"]),
                },
                "fx_rate": None if fx == 1 else {"rate": _float(fx, 6), "from": earnings.currency, "to": data.price_currency},
            },
            "warnings": warnings,
            "note": RULES,
        }

    def history(self, symbol: str, metric: str, years: int | None = None, as_of: str | None = None, points: int = 60) -> dict:
        """The metric's values over the window ending on as_of, thinned to at most `points` dates (the last day of each stretch)."""
        stock = self._stock(symbol)
        m = self._metric(stock, metric)
        years = self._years(years)
        if not 2 <= int(points) <= MAX_HISTORY_POINTS:
            raise RequestError(f"points must be between 2 and {MAX_HISTORY_POINTS}")
        asked = parse_date(as_of, "as_of")
        detail = self._load(stock).details[m.key]
        cutoff = min(asked, detail.index[-1]) if asked is not None else detail.index[-1]
        series = detail.loc[detail.index <= cutoff, "ratio"].dropna()
        if series.empty:
            raise RequestError(f"No {m.label} values for {stock.symbol} on or before {_day(cutoff)}")
        window = series[series.index >= series.index[-1] - pd.DateOffset(years=years)]
        step = max(1, -(-len(window) // int(points)))
        thinned = window.iloc[::-1].iloc[::step].iloc[::-1]  # always keeps the last day
        return {
            "symbol": stock.symbol,
            "metric": {"key": m.key, "label": m.label},
            "as_of": _day(cutoff),
            "window": {"start": _day(window.index[0]), "end": _day(window.index[-1]), "trading_days": len(window), "every_n_trading_days": step},
            "values": [{"date": _day(d), "value": _float(v)} for d, v in thinned.items()],
            "min": {"value": _float(window.min()), "date": _day(window.idxmin())},
            "max": {"value": _float(window.max()), "date": _day(window.idxmax())},  # the thinned values can skip the peak, so it is given here with its date
            "note": RULES + " The values are thinned and can miss the highest and lowest days: quote min and max from the fields of that name. Use get_valuation for the sources and the calculation of any single day.",
        }

    def filings(self, symbol: str, as_of: str | None = None, limit: int = 8) -> dict:
        """The earnings filings already effective on the as_of day (newest first), each with publication time, effective trading day and link."""
        stock = self._stock(symbol)
        asked = parse_date(as_of, "as_of")
        if not 1 <= int(limit) <= 50:
            raise RequestError("limit must be between 1 and 50")
        data = self._load(stock).data
        earnings = data.earnings
        rows = []
        for i, rec in enumerate(earnings.ttm_detail):
            available = pd.Timestamp(data.fundamentals.iloc[i]["available_date"])  # the first calendar day whose close may use the filing
            if asked is not None and available > asked:
                continue
            effective = effective_day(available, data.close.index)
            rows.append(
                {
                    "period": rec["period_label"],
                    "published": rec["published"],
                    "published_timezone": get_market(stock.market).timezone,
                    "effective_trading_day": None if effective is None else f"{effective:%Y-%m-%d}",
                    "filing_url": self._filing_url(earnings, rec),
                    "preliminary": bool(rec.get("provisional")),
                }
            )
        rows.sort(key=lambda r: (r["published"], r["period"]), reverse=True)
        return {"symbol": stock.symbol, "as_of": _day(asked), "filings": rows[: int(limit)], "available_before_limit": len(rows), "note": RULES}

    # ---- provenance ---------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _filing_url(earnings, rec: dict) -> str | None:
        for a in earnings.announcements:
            if period_label(a.period, earnings.periods_per_year) == rec["period_label"] and f"{a.published:%Y-%m-%d %H:%M}" == rec["published"]:
                return a.url
        return None

    @staticmethod
    def _components(earnings, rec: dict, column: str, cutoff: pd.Timestamp) -> list[dict]:
        """For each value that went into the per-share figure: its period, value, when it was first published and in which filing.

        Later filings repeat earlier figures; only those already in effect on the cutoff day count as confirmations.
        """
        known = {a.url for a in earnings.announcements if a.available <= cutoff}
        out = []
        label_of = lambda p: period_label(p, earnings.periods_per_year)  # noqa: E731

        def add(series: str, item: str, label: str, value: float | None, derived: bool = False) -> None:
            quarter = next((q for p, q in earnings.quarters[series].items() if label_of(p) == label), None)
            if quarter is None:
                return
            out.append(
                {
                    "item": item,
                    "period": label,
                    "value": _float(value if value is not None else quarter.value, 4),
                    "unit": UNITS[series],
                    "first_published": f"{quarter.published:%Y-%m-%d %H:%M}",
                    "filing_url": quarter.observations[0].url,
                    "confirming_filings": sum(1 for o in quarter.observations if o.url in known),
                    "derived_from_cumulative_figures": derived,
                }
            )

        for series, item in SOURCE_SERIES[column]:
            for comp in rec.get(series) or []:
                add(series, item, comp.period, comp.value, comp.derived)
        latest = rec["period_label"]
        if column == "book_value_per_share":
            add("equity", "Equity attributable to owners (period end)", latest, rec.get("equity"))
        if column in ("revenue_per_share_ttm", "book_value_per_share"):
            if rec.get("shares_source") == "implied":
                add("profit", "Net profit (used to imply the share count)", latest, rec.get("profit"))
                add("eps", "Basic EPS (used to imply the share count)", latest, rec.get("eps_latest"))
            else:
                add("wa_shares", "Weighted average shares", latest, None)
        return out


def open_service(config_path: str, data_dir: str, db_path: str | None = None, today: date | None = None, remote: RemoteDatabase | None = None) -> ValuationService:
    """A service on the saved data: database read-only, network off, nothing written (the request cache directory is never created).

    With `remote` (a bucket object, VALUATION_DB_URI on Cloud Run) the local copy is downloaded first and checked for a newer version every few minutes.
    """
    config = load_config(config_path)
    local = Path(db_path) if db_path else Path(data_dir) / "valuation.db"
    if remote is None:
        remote = RemoteDatabase.from_env(local)
    if remote is not None:
        remote.pull()
    monitor = Monitor(config, data_dir, Path(os.devnull), offline=True, db_path=local, readonly=True)
    return ValuationService(monitor, today, sync=remote.pull if remote else None)


def create_server(service: ValuationService, http: bool = False, host: str = "127.0.0.1", port: int = 8000):
    """Wrap the tools for the MCP library (installed with the `mcp` extra). http=True: requests are stateless and small, for serving on Cloud Run (see mcp_http.py)."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    http_options = {"host": host, "port": port, "stateless_http": True, "json_response": True, "max_request_body_size": 64 * 1024} if http else {}
    server = FastMCP(
        "valuation-monitor",
        **http_options,
        instructions=(
            "Look up how expensive a stock is compared with its own history (PE, PS, PB). Read-only and offline: the data is what the "
            "scheduled update saved from original filings and prices. "
            + RULES
            + " Always give the user the sources (filing link, publication time, effective trading day) and the calculation shown in the result "
            "so they can check it, and mention every warning."
        ),
    )
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=read_only)
    def list_stocks() -> dict:
        """List the stocks and metrics that can be asked about and how recent the saved data is. Call this first if unsure about symbols or metric keys."""
        return service.list_stocks()

    @server.tool(annotations=read_only)
    def get_valuation(symbol: str, metric: str, years: int | None = None, as_of: str | None = None) -> dict:
        """How high or low a valuation metric is compared with the stock's own history.

        symbol: for example 005930.KS. metric: pe, pe_non_ifrs, pe_non_gaap, ps or pb (see list_stocks). years: lookback window, default 10.
        as_of: YYYY-MM-DD; only data published and effective on or before that day is used (default: the newest saved day).
        Returns the value, its historical percentile and level, the statistics of the window, and the sources and calculation behind the number.
        """
        return service.valuation(symbol, metric, years, as_of)

    @server.tool(annotations=read_only)
    def get_history(symbol: str, metric: str, years: int | None = None, as_of: str | None = None, points: int = 60) -> dict:
        """The values of a metric over the lookback window ending on as_of, thinned to at most `points` dates, with the minimum and maximum."""
        return service.history(symbol, metric, years, as_of, points)

    @server.tool(annotations=read_only)
    def get_filings(symbol: str, as_of: str | None = None, limit: int = 8) -> dict:
        """The earnings filings already in effect on the as_of day (newest first): period, publication time, effective trading day and link to the original."""
        return service.filings(symbol, as_of, limit)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="valuation-monitor-mcp", description="MCP server: read-only, offline, sourced and point-in-time valuation lookups")
    parser.add_argument("-c", "--config", default=os.environ.get("VALUATION_CONFIG", "config.yaml"), help="Path to the config file")
    parser.add_argument("--data-dir", default=os.environ.get("VALUATION_DATA_DIR", "data"), help="Directory with valuation.db and filings/")
    parser.add_argument("--db", default=os.environ.get("VALUATION_DB_PATH") or None, help="Path of the SQLite database (default: valuation.db in the data directory)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio", help="stdio: started by the client on the same machine (default); streamable-http: a web service, for Cloud Run")
    parser.add_argument("--host", default="127.0.0.1", help="streamable-http only: address to listen on (0.0.0.0 on Cloud Run)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="streamable-http only: port (default: $PORT or 8000)")
    parser.add_argument("--per-client-limit", type=int, default=int(os.environ.get("VALUATION_MCP_PER_CLIENT_LIMIT", "60")), help="streamable-http only: requests per minute from one address")
    parser.add_argument("--total-limit", type=int, default=int(os.environ.get("VALUATION_MCP_TOTAL_LIMIT", "600")), help="streamable-http only: requests per minute from everyone")
    args = parser.parse_args(argv)
    try:
        service = open_service(args.config, args.data_dir, args.db)
    except FileNotFoundError as e:
        print(f"ERROR: {e}. Run valuation-monitor-update first, or point --data-dir / --db at a database (or set VALUATION_DB_URI).", file=sys.stderr)
        return 2
    if args.transport == "stdio":
        create_server(service).run()
    else:
        from valuation_monitor.mcp_http import serve

        serve(create_server(service, http=True, host=args.host, port=args.port), args.host, args.port, args.per_client_limit, args.total_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
