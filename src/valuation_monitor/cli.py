from __future__ import annotations

import argparse
import sys

import pandas as pd

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
    price_store = PriceStore(TencentQuoteProvider(http), YahooProvider(http), args.data_dir)
    loader = DataLoader(
        config,
        price_store,
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
        print(f"  股价核对  {price_summary(price_store.final_path(stock.symbol), config.lookback_years)}")
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


STATUS_LABELS = {"agree": "一致", "mismatch": "不一致", "tencent_only": "仅腾讯", "yahoo_only": "仅 Yahoo"}


def price_summary(final_path, lookback_years: int) -> str:
    """回看区间内腾讯证券与 Yahoo 的股价核对结果。"""
    final = pd.read_csv(final_path, index_col="date", parse_dates=True)
    final = final[final.index >= final.index[-1] - pd.DateOffset(years=lookback_years)]
    counts = final["status"].value_counts()
    parts = [f"{STATUS_LABELS[k]} {counts[k]} 天" for k in STATUS_LABELS if k in counts]
    return f"腾讯证券 vs Yahoo（还原除权调整后）：{'，'.join(parts)}（明细见 {final_path}）"
