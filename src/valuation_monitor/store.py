"""把下载和提取的数据保存在 data/ 目录（随代码提交到仓库），下次运行时直接复用。

  data/prices/<代码>.csv、<代码>.json   日线收盘价（未复权的实际成交价）；数据来源、拆股记录
  data/fx/<货币对>.csv、<货币对>.json   汇率
  data/eps/<代码>_announcements.csv     每份业绩公告的解析结果（公告原样数值）
  data/eps/<代码>_quarterly.csv         逐季 EPS（已按拆股调整），含交叉核对信息
  data/eps/<代码>_ttm.csv               每份公告发布后的 TTM EPS

行情只下载上次保存之后的新交易日，已保存的历史数据不会被覆盖。
当天的行情在收盘前还会变，所以不保存。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd

from valuation_monitor.filings import (
    PARSER_VERSION,
    EarningsHistory,
    adjust_for_splits,
    announcements_from_frame,
    announcements_to_frame,
    build_history,
    parse_announcement,
)
from valuation_monitor.providers.hkex import Filing
from valuation_monitor.providers.tencent import TencentQuoteProvider
from valuation_monitor.providers.yahoo import PriceHistory, YahooProvider

OVERLAP_DAYS = 7  # 增量下载时与已保存数据重叠的天数，用于核对数据源是否改动了历史数据
MISMATCH_TOLERANCE = 1e-4  # 相对差异
MARKET_TIMEZONE = "Asia/Hong_Kong"


def write_if_changed(path: Path, text: str) -> None:
    """内容没变就不写，避免文件无谓地出现在 git 改动里。"""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_meta(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_meta(path: Path, meta: dict) -> None:
    write_if_changed(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def split_adjust(close: pd.Series, splits: pd.Series) -> pd.Series:
    """把拆股前的价格换算到当前股本口径（与按拆股调整后的 EPS 一致）。"""
    out = close.copy()
    for day, ratio in splits.items():
        out[out.index < day] = out[out.index < day] / ratio
    return out


class PriceStore:
    def __init__(self, quotes: TencentQuoteProvider, yahoo: YahooProvider, root: str | Path = "data"):
        self.quotes = quotes
        self.yahoo = yahoo
        self.root = Path(root)

    def prices(self, symbol: str, start: datetime) -> PriceHistory:
        """收盘价（已按拆股换算到当前股本口径）和拆股记录。"""
        folder = self.root / "prices"
        close = self._series(
            folder / f"{symbol}.csv",
            folder / f"{symbol}.json",
            source=f"腾讯证券行情接口 {symbol}（未复权）",
            fetch=lambda since: self.quotes.daily_close(symbol, since),
            start=start,
            column="close",
            decimals=3,
        )
        splits = self._splits(symbol, folder / f"{symbol}.json", start)
        return PriceHistory(split_adjust(close, splits), splits, self.quotes.currency, self.quotes.timezone)

    def fx(self, from_ccy: str, to_ccy: str, start: datetime) -> pd.Series:
        """1 单位 from_ccy 兑换多少 to_ccy 的日线序列。"""
        pair = f"{from_ccy}{to_ccy}"
        folder = self.root / "fx"
        return self._series(
            folder / f"{pair}.csv",
            folder / f"{pair}.json",
            source=f"Yahoo Finance {pair}=X",
            fetch=lambda since: self.yahoo.price_history(f"{pair}=X", since).close,
            start=start,
            column="rate",
            decimals=6,
        )

    def _splits(self, symbol: str, meta_path: Path, start: datetime) -> pd.Series:
        """拆股记录来自 Yahoo（只用它的拆股事件，不用它的价格）。"""
        meta = read_meta(meta_path)
        known = {pd.Timestamp(d): r for d, r in meta.get("splits", {}).items()}
        try:
            # 从上次检查日往前 30 天开始查，既只下载少量数据，又不会漏掉刚公布的拆股
            since = (
                pd.Timestamp(meta["splits_checked"]) - pd.Timedelta(days=30)
                if "splits_checked" in meta
                else pd.Timestamp(start)
            )
            found = self.yahoo.price_history(symbol, since.to_pydatetime()).splits
        except Exception as e:
            print(f"{symbol}: 无法从 Yahoo 获取拆股记录（{e}），使用已保存的记录", file=sys.stderr)
            return pd.Series(known, dtype=float).sort_index()
        known.update(found.to_dict())
        meta["splits_source"] = f"Yahoo Finance {symbol}"
        meta["splits"] = {f"{d:%Y-%m-%d}": r for d, r in sorted(known.items())}
        meta["splits_checked"] = f"{pd.Timestamp.now(tz=MARKET_TIMEZONE):%Y-%m-%d}"
        write_meta(meta_path, meta)
        return pd.Series(known, dtype=float).sort_index()

    def _series(
        self,
        csv_path: Path,
        meta_path: Path,
        source: str,
        fetch: Callable[[datetime], pd.Series],
        start: datetime,
        column: str,
        decimals: int,
    ) -> pd.Series:
        stored = (
            pd.read_csv(csv_path, index_col="date", parse_dates=True)[column]
            if csv_path.exists()
            else pd.Series(dtype=float)
        )
        meta = read_meta(meta_path)
        start = pd.Timestamp(start).normalize()

        # fetched_from 记录已经下载过的最早日期；更早的部分（如调大了回看年限）才需要补下载
        fetched_from = pd.Timestamp(meta["fetched_from"]) if "fetched_from" in meta else None
        if stored.empty or fetched_from is None or start < fetched_from:
            fetch_from = start
        else:
            fetch_from = stored.index[-1] - pd.Timedelta(days=OVERLAP_DAYS)
        try:
            live = fetch(fetch_from.to_pydatetime())
        except Exception as e:
            if stored.empty:
                raise
            print(f"{source}: 无法更新（{e}），使用已保存的数据（截至 {stored.index[-1]:%Y-%m-%d}）", file=sys.stderr)
            return stored[stored.index >= start]

        today = pd.Timestamp.now(tz=MARKET_TIMEZONE).tz_localize(None).normalize()
        completed = live[live.index < today].round(decimals)
        overlap = completed.index.intersection(stored.index)
        changed = (completed[overlap] / stored[overlap] - 1).abs() > MISMATCH_TOLERANCE
        if changed.any():
            print(f"{source}: 有 {int(changed.sum())} 天的历史数据与已保存的不同，保留已保存的数据", file=sys.stderr)

        merged = pd.concat([stored, completed[~completed.index.isin(stored.index)]]).sort_index()
        write_if_changed(csv_path, merged.rename(column).to_csv(index_label="date", date_format="%Y-%m-%d"))
        meta["source"] = source
        meta["fetched_from"] = f"{min(fetch_from, fetched_from or fetch_from):%Y-%m-%d}"
        write_meta(meta_path, meta)

        result = pd.concat([merged, live[live.index >= today]])  # 加上当天的实时行情（不保存）
        return result[result.index >= start]


class EarningsStore:
    def __init__(self, root: str | Path = "data"):
        self.folder = Path(root) / "eps"

    def paths(self, symbol: str) -> dict[str, Path]:
        return {kind: self.folder / f"{symbol}_{kind}.csv" for kind in ("announcements", "quarterly", "ttm")}

    def load(self, symbol: str, filings: list[Filing], splits: pd.Series) -> EarningsHistory:
        """解析公告并计算 EPS。已解析过的公告直接读取保存的结果，只解析新公告。"""
        paths = self.paths(symbol)
        saved = {}
        if paths["announcements"].exists():
            df = pd.read_csv(paths["announcements"], dtype=str)
            df = df[df["parser_version"].astype(int) == PARSER_VERSION]
            saved = {a.url: a for a in announcements_from_frame(df)}

        parsed = [saved.get(f.url) or parse_announcement(f) for f in filings]
        history = build_history(adjust_for_splits(parsed, splits))

        everything = {**saved, **{a.url: a for a in parsed}}
        write_if_changed(paths["announcements"], announcements_to_frame(list(everything.values())).to_csv(index=False))
        write_if_changed(paths["quarterly"], history.quarters_frame().to_csv(index=False))
        write_if_changed(paths["ttm"], history.ttm.to_csv(index=False, date_format="%Y-%m-%d"))
        return history
