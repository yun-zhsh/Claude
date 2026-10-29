"""把下载和提取的数据保存在 data/ 目录（随代码提交到仓库），下次运行时直接复用。

  data/prices/<代码>/                   各来源的日线原始数据，以及综合判定后的收盘价（见 PriceStore）
  data/fx/<货币对>.csv、<货币对>.json   汇率
  data/eps/<代码>_announcements.csv     每份业绩公告的解析结果（公告原样数值）
  data/eps/<代码>_periods.csv           逐期（季度或半年）业绩（EPS 已按拆股调整），含交叉核对信息
  data/eps/<代码>_ttm.csv               每份公告发布后的 TTM EPS

行情只下载上次保存之后的新交易日。原始成交价和汇率保存后不再改动；Yahoo 的
除权调整价会随新的除权回溯变化，发现变化时整段重新下载。当天的行情在收盘前还会变，所以不保存。
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

OVERLAP_DAYS = 7  # 增量下载时与已保存数据重叠的天数，用于核对数据源是否改动了历史数据
MISMATCH_TOLERANCE = 1e-4  # 相对差异


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


BAR_DECIMALS = {"open": 3, "high": 3, "low": 3, "close": 3, "volume": 0}
YAHOO_DECIMALS = {"open": 4, "high": 4, "low": 4, "close": 4, "adjclose": 4, "volume": 0}


class PriceStore:
    """每个来源的原始数据分别保存，再综合判定出最终采用的收盘价：

    data/prices/<代码>/tencent.csv   腾讯证券日线（未复权的实际成交价）
    data/prices/<代码>/yahoo.csv     Yahoo 日线（close 为除权调整价，adjclose 再按分红调整）
    data/prices/<代码>/yahoo.json    含 Yahoo 的拆股记录
    data/prices/<代码>/final.csv     综合判定结果：每天采用的价格、来源、核对状态
    """

    def __init__(self, quotes: TencentQuoteProvider, yahoo: YahooProvider, root: str | Path = "data"):
        self.quotes = quotes
        self.yahoo = yahoo
        self.root = Path(root)

    def final_path(self, symbol: str) -> Path:
        return self.root / "prices" / symbol / "final.csv"

    def prices(self, symbol: str, start: datetime, market: Market = HK) -> PriceHistory:
        """最终采用的收盘价（已按拆股换算到当前股本口径）和拆股记录。

        港股有腾讯证券（实际成交价）和 Yahoo 两个来源，互相核对；美股、韩股只用 Yahoo 一个来源，
        Yahoo 的收盘价已经按拆股调整，不再重复调整。
        """
        folder = self.root / "prices" / symbol
        if market.cross_checks_prices:
            tencent = self._frame(
                folder / "tencent.csv",
                folder / "tencent.json",
                source=f"腾讯证券行情接口 {symbol}（未复权）",
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
                frozen=False,  # Yahoo 的调整价会因新的除权回溯变化，发现变化时整段重新下载
                timezone=market.timezone,
            )
        except Exception as e:
            if not market.cross_checks_prices:
                raise
            print(f"Yahoo Finance {symbol}: 无法获取（{e}），只用腾讯证券的数据", file=sys.stderr)
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
        """1 单位 from_ccy 兑换多少 to_ccy 的日线序列。"""
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
            timezone="Europe/London",  # Yahoo 的外汇日线按伦敦时间划分
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
        """读取已保存的数据，只下载新增的交易日并保存。返回已保存的全部数据加当天行情。

        frozen=True：已保存的数据不再改动（原始成交价、汇率），数据源改动历史时只提示；
        frozen=False：数据源回溯调整了历史（如 Yahoo 的除权调整价）时，整段重新下载。
        timezone：数据源划分交易日所用的时区，用来判断哪些日子已经收盘、可以保存。
        """
        columns = list(decimals)
        check = "close" if "close" in decimals else columns[0]
        stored = pd.read_csv(csv_path, index_col="date", parse_dates=True) if csv_path.exists() else None
        if stored is None or set(columns) - set(stored.columns):  # 没有数据或格式已变
            stored = pd.DataFrame(columns=columns, dtype=float)
        meta = read_meta(meta_path)
        start = pd.Timestamp(start).normalize()
        today = pd.Timestamp.now(tz=timezone).tz_localize(None).normalize()

        # fetched_from 记录已经下载过的最早日期；更早的部分（如调大了回看年限）才需要补下载
        fetched_from = pd.Timestamp(meta["fetched_from"]) if "fetched_from" in meta and not stored.empty else None
        full = fetched_from is None or start < fetched_from
        fetch_from = min(start, fetched_from) if fetched_from is not None else start
        try:
            live = fetch((fetch_from if full else stored.index[-1] - pd.Timedelta(days=OVERLAP_DAYS)).to_pydatetime())
            completed = live[live.index < today][columns].round(decimals)
            overlap = completed.index.intersection(stored.index)
            changed = (completed.loc[overlap, check] / stored.loc[overlap, check] - 1).abs() > MISMATCH_TOLERANCE
            if changed.any() and frozen:
                print(f"{source}: 有 {int(changed.sum())} 天的历史数据与已保存的不同，保留已保存的数据", file=sys.stderr)
            elif changed.any():
                print(f"{source}: 历史数据被回溯调整（可能有新的除权），重新下载全部历史", file=sys.stderr)
                live = fetch(fetch_from.to_pydatetime())
                completed = live[live.index < today][columns].round(decimals)
                stored = stored.iloc[0:0]
        except Exception as e:
            if stored.empty:
                raise
            print(f"{source}: 无法更新（{e}），使用已保存的数据（截至 {stored.index[-1]:%Y-%m-%d}）", file=sys.stderr)
            return stored

        merged = pd.concat([stored, completed[~completed.index.isin(stored.index)]]).sort_index()
        for col, places in decimals.items():
            if places == 0:
                merged[col] = merged[col].round().astype("Int64")
        write_if_changed(csv_path, merged.to_csv(index_label="date", date_format="%Y-%m-%d"))
        meta["source"] = source
        meta["fetched_from"] = f"{fetch_from:%Y-%m-%d}"
        write_meta(meta_path, meta)

        result = pd.concat([merged.astype(float), live[live.index >= today][columns]])  # 加上当天的实时行情（不保存）
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
        """解析公告并计算 EPS。已解析过的公告直接读取保存的结果，只解析新公告。

        trading_days 用于在保存的明细中列出每个数值实际开始使用的交易日；fmt 是公告的版式。
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
                print(f"{symbol}: 无法解析暂用的初步数据 {f.url}（{e}），等正式财报", file=sys.stderr)
        history = build_history(adjust_for_splits(parsed, splits), splits)

        write_if_changed(paths["announcements"], announcements_to_frame(parsed, version).to_csv(index=False))
        write_if_changed(paths["periods"], history.periods_frame(trading_days).to_csv(index=False))
        write_if_changed(paths["ttm"], history.ttm_frame(trading_days).to_csv(index=False, date_format="%Y-%m-%d"))
        return history
