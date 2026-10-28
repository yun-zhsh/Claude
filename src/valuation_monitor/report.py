"""文字报告和走势图。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from valuation_monitor.analysis import Valuation
from valuation_monitor.config import StockConfig
from valuation_monitor.metrics import Metric


def format_text(stock: StockConfig, metric: Metric, v: Valuation) -> str:
    bands = "  ".join(f"P{p}={val:.1f}" for p, val in v.bands.items())
    return "\n".join(
        [
            f"{stock.name} ({stock.symbol})  {metric.label}",
            f"  日期      {v.as_of:%Y-%m-%d}",
            f"  当前值    {v.current:.2f}",
            f"  历史分位  {v.percentile:.1f}%  →  {v.level}",
            f"  回看区间  {v.window_start:%Y-%m-%d} ~ {v.as_of:%Y-%m-%d}（{v.observations} 个交易日）",
            f"  区间统计  最低 {v.min:.1f} / 均值 {v.mean:.1f} / 最高 {v.max:.1f}",
            f"  分位参考  {bands}",
        ]
    )


def plot(stock: StockConfig, metric: Metric, series: pd.Series, v: Valuation, out_dir: str | Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    window = series[series.index >= v.window_start]
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(window.index, window.to_numpy(), color="#2563eb", linewidth=1.2, label=metric.label)
    styles = {10: ("#16a34a", ":"), 30: ("#16a34a", "--"), 50: ("#6b7280", "-"), 70: ("#dc2626", "--"), 90: ("#dc2626", ":")}
    for p, val in v.bands.items():
        color, ls = styles.get(p, ("#6b7280", "--"))
        ax.axhline(val, color=color, linestyle=ls, linewidth=0.9, label=f"P{p} = {val:.1f}")
    ax.scatter([v.as_of], [v.current], color="#f59e0b", zorder=5, label=f"Now {v.current:.1f} (P{v.percentile:.0f})")
    ax.set_title(f"{stock.symbol} {metric.label}  {v.window_start:%Y-%m} ~ {v.as_of:%Y-%m-%d}")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stock.symbol}_{metric.key}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
