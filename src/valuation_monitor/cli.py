from __future__ import annotations

import argparse
import sys

from valuation_monitor.config import load_config
from valuation_monitor.report import format_text, plot
from valuation_monitor.service import PRICE_STATUS_LABELS, Monitor


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to the config file (default: config.yaml)")
    parser.add_argument("--data-dir", default="data", help="Directory where prices, filings and EPS data are saved (default: data)")
    parser.add_argument("--refresh", action="store_true", help="Ignore the 12-hour network request cache")
    parser.add_argument("--cache-dir", default=".cache", help="Directory for the request cache (default: .cache)")


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
