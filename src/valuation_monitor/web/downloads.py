"""生成供下载的文件：列名换成中文，CSV 带 BOM 以便 Excel 直接打开不乱码。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd

from valuation_monitor.service import PRICE_STATUS_LABELS, Monitor

PRICE_COLUMNS = {
    "date": "日期",
    "close": "收盘价（实际成交价）",
    "source": "采用来源",
    "status": "核对状态",
    "tencent_close": "腾讯证券收盘价",
    "yahoo_close_unadjusted": "Yahoo 收盘价（还原除权调整后）",
    "yahoo_factor": "Yahoo 调整系数",
}
SOURCE_LABELS = {"tencent": "腾讯证券", "yahoo": "Yahoo"}

# 逐季明细：每项数据都有数值、首次公布时间、生效交易日、证实公告数、各公告最大偏差
SERIES_NAMES = {  # 系列 -> (带单位的完整名称, 其他列名里用的简称，必须互不相同)
    "eps": ("基本 EPS（IFRS/GAAP，元/股；ADR 为元/份 ADS）", "IFRS EPS"),
    "non_ifrs_eps": ("基本 EPS（Non-IFRS/Non-GAAP，元/股；ADR 为元/份 ADS）", "Non-IFRS EPS"),
    "revenue": ("营收（百万元）", "营收"),
    "profit": ("归母净利润（百万元）", "归母净利润"),
    "equity": ("期末归母净资产（百万元）", "期末归母净资产"),
    "wa_shares": ("公告披露的股数（百万股；加权平均股数，A 股为期末总股本；ADR 为百万份 ADS）", "公告披露股数"),
}
PERIOD_COLUMNS = {"period": "报告期（季度或半年）", "currency": "币种"}
for _key, (_name, _short) in SERIES_NAMES.items():
    PERIOD_COLUMNS |= {
        _key: _name,
        f"{_key}_published": f"{_short} 首次公布时间",
        f"{_key}_effective": f"{_short} 生效交易日",
        f"{_key}_sources": f"{_short} 证实公告数",
        f"{_key}_max_deviation": f"{_short} 各公告最大偏差",
    }
PERIOD_COLUMNS |= {"shares": "反推股数（对照用：归母净利润 ÷ 基本 EPS，百万股）", "first_source": "最早公布该期数据的公告"}

TTM_COLUMNS = {
    "report_date": "报告期末",
    "published": "公告发布时间（交易所所在地时间）",
    "available_date": "可用日期（自然日）",
    "effective_date": "生效交易日",
    "provisional": "是否暂用初步数据（1=业绩发布会材料，财报发布后替换）",
    "revenue_ttm": "TTM 营收 = 各期营收之和（百万）",
    "shares": "股数（百万股；ADR 为百万份 ADS）：优先用公告披露的加权平均股数，没有才反推",
    "shares_source": "股数来源（公告披露 / 由全年数推算 / 反推）",
    "shares_implied": "对照：反推股数 = 最新期归母净利润 ÷ 最新期基本 EPS",
    "profit_latest": "最新期归母净利润（百万）",
    "eps_latest": "最新期基本 EPS",
    "eps_ttm": "TTM EPS = 各期基本 EPS 之和",
    "eps_ttm_formula": "TTM EPS 算式（* 表示由累计数相减得到）",
    "non_ifrs_eps_ttm": "TTM Non-IFRS/Non-GAAP EPS = 各期之和",
    "non_ifrs_eps_ttm_formula": "TTM Non-IFRS/Non-GAAP EPS 算式",
    "revenue_ttm_formula": "TTM 营收算式",
    "shares_formula": "股数算式",
    "revenue_per_share_ttm": "每股营收 TTM = TTM 营收 ÷ 股数（财报币种）",
    "revenue_per_share_formula": "每股营收算式",
    "equity": "最新期末归母净资产（百万）",
    "book_value_per_share": "每股净资产 = 期末归母净资产 ÷ 股数（财报币种）",
    "book_value_per_share_formula": "每股净资产算式",
    "split_note": "拆股调整说明（公告原文数值 → 调整后）",
    "published_note": "发布时间说明",
}
for _k in range(1, 5):  # TTM 覆盖的各期（从早到晚）及各期数值，加总即得 TTM
    TTM_COLUMNS |= {
        f"period_{_k}": f"TTM 第{_k}期",
        f"eps_{_k}": f"第{_k}期 基本 EPS",
        f"non_ifrs_eps_{_k}": f"第{_k}期 Non-IFRS/Non-GAAP EPS",
        f"revenue_{_k}": f"第{_k}期 营收（百万）",
    }

# 网址中的文件名 -> 下载时保存的文件名（不含股票代码）
FILES = {
    "prices.csv": "历史股价.csv",
    "eps.csv": "历史业绩.csv",
    "ttm.csv": "历史业绩_每股指标.csv",
    "valuation.csv": "每日估值明细.csv",
    "raw.zip": "全部原始数据.zip",
}


def download_name(symbol: str, name: str) -> str:
    return f"{symbol}_{FILES[name]}"


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def drop_empty_series(df: pd.DataFrame) -> pd.DataFrame:
    """去掉该公司没有披露的数据系列（如舜宇没有 Non-IFRS EPS）的全部列。"""
    empty = [key for key in SERIES_NAMES if key in df and df[key].isna().all()]
    suffixes = ("", "_published", "_effective", "_sources", "_max_deviation")
    return df.drop(columns=[f"{key}{suffix}" for key in empty for suffix in suffixes])


def build(monitor: Monitor, symbol: str, name: str, report=None) -> bytes:
    if name == "prices.csv":
        df = pd.read_csv(monitor.prices.final_path(symbol)).dropna(axis=1, how="all")  # 美股没有腾讯证券的数据
        df["status"] = df["status"].map(PRICE_STATUS_LABELS)
        df["source"] = df["source"].map(SOURCE_LABELS)
        return to_csv_bytes(df.rename(columns=PRICE_COLUMNS))
    if name == "eps.csv":
        df = drop_empty_series(pd.read_csv(monitor.earnings.paths(symbol)["periods"]))
        return to_csv_bytes(df.rename(columns=PERIOD_COLUMNS))
    if name == "ttm.csv":
        df = pd.read_csv(monitor.earnings.paths(symbol)["ttm"]).dropna(axis=1, how="all")
        return to_csv_bytes(df.rename(columns=TTM_COLUMNS))
    if name == "valuation.csv":
        return to_csv_bytes(valuation_frame(monitor, symbol, report))
    if name == "raw.zip":
        return raw_zip(monitor.data_dir, symbol)
    raise KeyError(name)


def raw_zip(data_dir: Path, symbol: str) -> bytes:
    """data/ 目录中与该股票相关的原始数据文件（公告原文除外，只含其索引）。"""
    files = [
        *sorted((data_dir / "prices" / symbol).glob("*")),
        *sorted((data_dir / "eps").glob(f"{symbol}_*")),
        *sorted((data_dir / "fx").glob("*")),
        data_dir / "filings" / symbol / "index.json",
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if f.is_file():
                zf.write(f, f.relative_to(data_dir).as_posix())
    return buf.getvalue()


def valuation_frame(monitor: Monitor, symbol: str, report) -> pd.DataFrame:
    """每个交易日、每项指标的完整计算过程：比率 = 收盘价 ÷ （每股数据 × 汇率）。

    “业绩记录”指当天采用的是哪一期业绩（和哪天发布的），对应 TTM 文件里的同一行，那里有每股数据的算式。
    """
    data = report.data
    earnings = data.earnings
    out = pd.DataFrame({"日期": data.close.index, "收盘价（股价币种）": data.close.to_numpy()})
    final_path = monitor.prices.final_path(symbol)
    if final_path.exists():
        final = pd.read_csv(final_path, index_col="date", parse_dates=True)
        out["价格来源"] = final["source"].map(SOURCE_LABELS).reindex(data.close.index).to_numpy()
    out["汇率（1 单位财报币种 = 多少股价币种）"] = data.fx.to_numpy()
    for r in report.metrics:
        if r.error or not hasattr(r.metric, "detail"):
            continue
        d = r.metric.detail(data)
        fundamentals = data.fundamentals.reset_index(drop=True)
        rec = d["record"]
        known = rec.notna()
        idx = rec[known].astype(int)
        label = r.metric.label
        out[f"{label}｜业绩记录（报告期）"] = pd.Series(
            [earnings.ttm_detail[i]["period_label"] for i in idx], index=idx.index
        ).reindex(d.index).to_numpy() if earnings and earnings.ttm_detail else None
        out[f"{label}｜业绩记录发布时间"] = pd.Series(fundamentals["published"].reindex(idx).to_numpy(), index=idx.index).reindex(d.index).to_numpy()
        out[f"{label}｜每股数据（财报币种）"] = d["per_share"].to_numpy()
        out[f"{label}｜每股数据（换算成股价币种）= 每股数据 × 汇率"] = d["per_share_converted"].to_numpy()
        out[f"{label}｜比率 = 收盘价 ÷ 换算后每股数据"] = d["ratio"].to_numpy()
    return out
