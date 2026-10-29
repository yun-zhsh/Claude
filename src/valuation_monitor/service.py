"""对每只股票取数、计算各项估值指标，供命令行和网页共用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from valuation_monitor.analysis import Valuation, evaluate, level_index
from valuation_monitor.config import Config, StockConfig
from valuation_monitor.data import DataLoader, StockData
from valuation_monitor.metrics import METRICS, Metric
from valuation_monitor.providers.hkex import HkexProvider
from valuation_monitor.providers.http import HttpClient
from valuation_monitor.providers.samsung import SamsungIrProvider
from valuation_monitor.providers.sec import SecProvider, SecUserAgentMissing, make_http_client
from valuation_monitor.providers.tencent import TencentQuoteProvider
from valuation_monitor.providers.yahoo import YahooProvider
from valuation_monitor.store import EarningsStore, PriceStore

PRICE_STATUS_LABELS = {"agree": "一致", "mismatch": "不一致", "tencent_only": "仅腾讯", "yahoo_only": "仅 Yahoo"}


@dataclass
class WindowResult:
    """某个回看年数下的估值。"""

    valuation: Valuation
    level_index: int  # 第几档（0 为最低估），用于配色


@dataclass
class MetricResult:
    metric: Metric
    series: pd.Series | None = None
    windows: dict[int, WindowResult] = field(default_factory=dict)  # 回看年数 -> 结果
    default_years: int = 10
    error: str | None = None

    @property
    def valuation(self) -> Valuation | None:
        """默认回看年数下的估值。"""
        w = self.windows.get(self.default_years)
        return w.valuation if w else None

    @property
    def level_index(self) -> int | None:
        w = self.windows.get(self.default_years)
        return w.level_index if w else None


@dataclass
class StockReport:
    stock: StockConfig
    updated_at: datetime
    data: StockData | None = None
    metrics: list[MetricResult] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)
    price_check: dict[str, int] = field(default_factory=dict)  # 回看区间内各核对状态的天数
    error: str | None = None

    @property
    def latest_close(self) -> tuple[pd.Timestamp, float] | None:
        if self.data is None or self.data.close.empty:
            return None
        return self.data.close.index[-1], float(self.data.close.iloc[-1])


class Monitor:
    def __init__(self, config: Config, data_dir: str | Path = "data", cache_dir: str | Path = ".cache", refresh: bool = False):
        self.config = config
        self.data_dir = Path(data_dir)
        http = HttpClient(cache_dir=cache_dir, refresh=refresh)
        self.prices = PriceStore(TencentQuoteProvider(http), YahooProvider(http), self.data_dir)
        self.earnings = EarningsStore(self.data_dir)
        try:
            sec_http = make_http_client(cache_dir, refresh=refresh)
        except SecUserAgentMissing:
            sec_http = None  # 没有声明使用者时不访问美国证监会，直接使用本地已保存的公告
        self.loader = DataLoader(
            config,
            self.prices,
            HkexProvider(http, self.data_dir / "filings"),
            SecProvider(sec_http, self.data_dir / "filings"),
            SamsungIrProvider(http, self.data_dir / "filings"),
            self.earnings,
        )

    def analyze(self, stock: StockConfig) -> StockReport:
        report = StockReport(stock=stock, updated_at=datetime.now())
        try:
            report.data = self.loader.load(stock)
        except Exception as e:  # 单只股票失败不影响其他股票
            report.error = f"数据获取失败: {e}"
            return report

        report.discrepancies = report.data.earnings.discrepancies()
        report.price_check = self.price_check(stock.symbol)
        for key in stock.metrics:
            result = MetricResult(METRICS[key], default_years=self.config.lookback_years)
            try:
                result.series = result.metric.compute(report.data)
                for years in self.config.lookback_options:
                    v = evaluate(result.series, years, self.config.levels)
                    result.windows[years] = WindowResult(v, level_index(v.percentile, self.config.levels))
            except Exception as e:
                result.error = f"计算失败: {e}"
            report.metrics.append(result)
        return report

    def analyze_all(self) -> list[StockReport]:
        return [self.analyze(stock) for stock in self.config.stocks]

    def price_check(self, symbol: str) -> dict[str, int]:
        path = self.prices.final_path(symbol)
        if not path.exists():
            return {}
        final = pd.read_csv(path, index_col="date", parse_dates=True)
        final = final[final.index >= final.index[-1] - pd.DateOffset(years=self.config.max_lookback)]
        counts = final["status"].value_counts()
        return {k: int(counts[k]) for k in PRICE_STATUS_LABELS if k in counts}
