from __future__ import annotations

import argparse
import sys

from valuation_monitor.analysis import evaluate
from valuation_monitor.config import load_config
from valuation_monitor.data import DataLoader
from valuation_monitor.metrics import METRICS
from valuation_monitor.providers.hkex import HkexProvider
from valuation_monitor.providers.http import HttpClient
from valuation_monitor.providers.tencent import TencentQuoteProvider
from valuation_monitor.providers.yahoo import YahooProvider
from valuation_monitor.report import format_text, plot
from valuation_monitor.store import EarningsStore, PriceStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="valuation-monitor", description="监控股票估值处于历史高位还是低位")
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--plot", metavar="DIR", help="把指标走势图保存到该目录")
    parser.add_argument("--data-dir", default="data", help="行情、公告、EPS 的保存目录（默认 data）")
    parser.add_argument("--refresh", action="store_true", help="忽略 12 小时的网络请求缓存")
    parser.add_argument("--cache-dir", default=".cache", help="数据缓存目录（默认 .cache）")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    http = HttpClient(cache_dir=args.cache_dir, refresh=args.refresh)
    earnings_store = EarningsStore(args.data_dir)
    loader = DataLoader(
        config,
        PriceStore(TencentQuoteProvider(http), YahooProvider(http), args.data_dir),
        HkexProvider(http, f"{args.data_dir}/filings"),
        earnings_store,
    )

    failed = False
    for stock in config.stocks:
        try:
            data = loader.load(stock)
        except Exception as e:  # 单只股票失败不影响其他股票
            print(f"{stock.name} ({stock.symbol}): 数据获取失败: {e}", file=sys.stderr)
            failed = True
            continue

        earnings = data.earnings
        print(
            f"{stock.name} ({stock.symbol})  EPS 来源：港交所披露易 {len(earnings.announcements)} 份业绩公告原文，"
            f"币种 {earnings.currency}"
        )
        for issue in earnings.discrepancies():
            print(f"  ⚠ {issue}")
        print(f"  EPS 明细  {earnings_store.paths(stock.symbol)['quarterly']}")
        print()

        for key in stock.metrics:
            metric = METRICS[key]
            try:
                series = metric.compute(data)
                v = evaluate(series, config.lookback_years, config.levels)
            except Exception as e:
                print(f"{stock.name} ({stock.symbol}) {metric.label}: 计算失败: {e}", file=sys.stderr)
                failed = True
                continue
            print(format_text(stock, metric, v))
            if args.plot:
                print(f"  走势图    {plot(stock, metric, series, v, args.plot)}")
            print()
    return 1 if failed else 0
