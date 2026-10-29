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
    "eps": ("基本 EPS（IFRS，元/股）", "IFRS EPS"),
    "non_ifrs_eps": ("基本 EPS（Non-IFRS，元/股）", "Non-IFRS EPS"),
    "revenue": ("营收（百万元）", "营收"),
    "profit": ("归母净利润（百万元）", "归母净利润"),
    "equity": ("期末归母净资产（百万元）", "期末归母净资产"),
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
PERIOD_COLUMNS |= {"shares": "反推股数（百万股，归母净利润 ÷ 基本 EPS）", "first_source": "最早公布该期数据的公告"}

TTM_COLUMNS = {
    "report_date": "报告期末",
    "published": "公告发布时间（香港时间）",
    "available_date": "可用日期（自然日）",
    "effective_date": "生效交易日",
    "eps_ttm": "TTM EPS（IFRS，元/股）",
    "non_ifrs_eps_ttm": "TTM EPS（Non-IFRS，元/股）",
    "revenue_ttm": "TTM 营收（百万元）",
    "shares": "股数（百万股，最新一期加权平均）",
    "revenue_per_share_ttm": "每股营收 TTM（元/股）",
    "equity": "期末归母净资产（百万元）",
    "book_value_per_share": "每股净资产（元/股）",
}

# 网址中的文件名 -> 下载时保存的文件名（不含股票代码）
FILES = {
    "prices.csv": "历史股价.csv",
    "eps.csv": "历史业绩.csv",
    "ttm.csv": "历史业绩_每股指标.csv",
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


def build(monitor: Monitor, symbol: str, name: str) -> bytes:
    if name == "prices.csv":
        df = pd.read_csv(monitor.prices.final_path(symbol))
        df["status"] = df["status"].map(PRICE_STATUS_LABELS)
        df["source"] = df["source"].map(SOURCE_LABELS)
        return to_csv_bytes(df.rename(columns=PRICE_COLUMNS))
    if name == "eps.csv":
        df = drop_empty_series(pd.read_csv(monitor.earnings.paths(symbol)["periods"]))
        return to_csv_bytes(df.rename(columns=PERIOD_COLUMNS))
    if name == "ttm.csv":
        df = pd.read_csv(monitor.earnings.paths(symbol)["ttm"]).dropna(axis=1, how="all")
        return to_csv_bytes(df.rename(columns=TTM_COLUMNS))
    if name == "raw.zip":
        return raw_zip(monitor.data_dir, symbol)
    raise KeyError(name)


def raw_zip(data_dir: Path, symbol: str) -> bytes:
    """data/ 目录中与该股票相关的原始数据文件（公告 PDF 除外，只含其索引）。"""
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
