from __future__ import annotations

import argparse
import sys

from valuation_monitor.config import load_config
from valuation_monitor.report import format_text, plot
from valuation_monitor.service import PRICE_STATUS_LABELS, Monitor


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--data-dir", default="data", help="行情、公告、EPS 的保存目录（默认 data）")
    parser.add_argument("--refresh", action="store_true", help="忽略 12 小时的网络请求缓存")
    parser.add_argument("--cache-dir", default=".cache", help="数据缓存目录（默认 .cache）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="valuation-monitor", description="监控股票估值处于历史高位还是低位")
    add_common_args(parser)
    parser.add_argument("--plot", metavar="DIR", help="把指标走势图保存到该目录")
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
            f"{stock.name} ({stock.symbol})  EPS 来源：港交所披露易 {len(earnings.announcements)} 份业绩公告原文，"
            f"币种 {earnings.currency}"
        )
        for issue in report.discrepancies:
            print(f"  ⚠ {issue}")
        print(f"  EPS 明细  {monitor.earnings.paths(stock.symbol)['quarterly']}")
        checks = "，".join(f"{PRICE_STATUS_LABELS[k]} {n} 天" for k, n in report.price_check.items())
        print(f"  股价核对  腾讯证券 vs Yahoo（还原除权调整后）：{checks}（明细见 {monitor.prices.final_path(stock.symbol)}）")
        print()

        for result in report.metrics:
            if result.error:
                print(f"{stock.name} ({stock.symbol}) {result.metric.label}: {result.error}", file=sys.stderr)
                failed = True
                continue
            print(format_text(stock, result.metric, result.valuation))
            if args.plot:
                print(f"  走势图    {plot(stock, result.metric, result.series, result.valuation, args.plot)}")
            print()
    return 1 if failed else 0
