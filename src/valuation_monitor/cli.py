from __future__ import annotations

import argparse
import os
import sys

from valuation_monitor.config import load_config
from valuation_monitor.report import format_text, plot
from valuation_monitor.service import PRICE_STATUS_LABELS, Monitor


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Options shared by all commands. The defaults can be set through environment variables (used by the container image):
    VALUATION_CONFIG, VALUATION_DATA_DIR and VALUATION_CACHE_DIR."""
    config = os.environ.get("VALUATION_CONFIG", "config.yaml")
    data_dir = os.environ.get("VALUATION_DATA_DIR", "data")
    cache_dir = os.environ.get("VALUATION_CACHE_DIR", ".cache")
    parser.add_argument("-c", "--config", default=config, help=f"Path to the config file (default: {config})")
    parser.add_argument("--data-dir", default=data_dir, help=f"Directory where prices, filings and EPS data are saved (default: {data_dir})")
    parser.add_argument("--refresh", action="store_true", help="Ignore the 12-hour network request cache")
    parser.add_argument("--cache-dir", default=cache_dir, help=f"Directory for the request cache (default: {cache_dir})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="valuation-monitor", description="Monitor whether a stock's valuation is high or low relative to its own history")
    add_common_args(parser)
    parser.add_argument("--plot", metavar="DIR", help="Save the metric trend charts into this directory")
    args = parser.parse_args(argv)

    monitor = Monitor(load_config(args.config), args.data_dir, args.cache_dir, args.refresh)
    failed = False
    for report in monitor.analyze_all():
        stock = report.stock
        if report.error:
            print(f"{stock.name} ({stock.symbol}): {report.error}", file=sys.stderr)
            failed = True
            continue

        earnings = report.data.earnings
        print(
            f"{stock.name} ({stock.symbol})  Earnings source: {stock.filing_source}, {len(earnings.announcements)} original filings, "
            f"currency {earnings.currency}"
        )
        for issue in report.discrepancies:
            print(f"  ⚠ {issue}")
        print(f"  Earnings detail  {monitor.earnings.paths(stock.symbol)['periods']}")
        if stock.cross_checks_prices:
            checks = ", ".join(f"{PRICE_STATUS_LABELS[k]} {n} days" for k, n in report.price_check.items())
            print(f"  Price check  Tencent vs Yahoo (after undoing Yahoo's adjustments): {checks} (details in {monitor.prices.final_path(stock.symbol)})")
        else:
            print(f"  Price source  Yahoo Finance only, no second source to cross-check (details in {monitor.prices.final_path(stock.symbol)})")
        print()

        for result in report.metrics:
            if result.error:
                print(f"{stock.name} ({stock.symbol}) {result.metric.label}: {result.error}", file=sys.stderr)
                failed = True
                continue
            print(format_text(stock, result.metric, result.valuation))
            if args.plot:
                print(f"  Chart  {plot(stock, result.metric, result.series, result.valuation, args.plot)}")
            print()
    return 1 if failed else 0


def update_main(argv: list[str] | None = None) -> int:
    """Fetch the latest prices and filings and save them, without printing reports (for scheduled jobs)."""
    parser = argparse.ArgumentParser(prog="valuation-monitor-update", description="Update the saved prices and filings")
    add_common_args(parser)
    args = parser.parse_args(argv)

    print(f"Data directory: {args.data_dir}")
    monitor = Monitor(load_config(args.config), args.data_dir, args.cache_dir, args.refresh)
    failed = False
    for report in monitor.analyze_all():
        if report.error:
            print(f"{report.stock.name} ({report.stock.symbol}): {report.error}", file=sys.stderr)
            failed = True
        else:
            last = report.latest_close
            print(f"{report.stock.name} ({report.stock.symbol}): updated, latest close {last[1]:.2f} on {last[0]:%Y-%m-%d}" if last else f"{report.stock.name}: updated")
    return 1 if failed else 0
