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

QUARTERLY_COLUMNS = {
    "quarter": "季度",
    "currency": "币种",
    "eps": "基本 EPS（IFRS）",
    "eps_available": "IFRS EPS 可用日期",
    "eps_sources": "IFRS EPS 证实公告数",
    "eps_max_deviation": "IFRS EPS 各公告最大偏差",
    "non_ifrs_eps": "基本 EPS（Non-IFRS）",
    "non_ifrs_eps_available": "Non-IFRS EPS 可用日期",
    "non_ifrs_eps_sources": "Non-IFRS EPS 证实公告数",
    "non_ifrs_eps_max_deviation": "Non-IFRS EPS 各公告最大偏差",
    "first_source": "最早公布该数据的公告",
}
TTM_COLUMNS = {
    "report_date": "报告期末",
    "available_date": "可用日期",
    "eps_ttm": "TTM EPS（IFRS）",
    "non_ifrs_eps_ttm": "TTM EPS（Non-IFRS）",
}

# 网址中的文件名 -> 下载时保存的文件名（不含股票代码）
FILES = {
    "prices.csv": "历史股价.csv",
    "eps.csv": "历史业绩_逐季EPS.csv",
    "ttm.csv": "历史业绩_TTM_EPS.csv",
    "raw.zip": "全部原始数据.zip",
}


def download_name(symbol: str, name: str) -> str:
    return f"{symbol}_{FILES[name]}"


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def build(monitor: Monitor, symbol: str, name: str) -> bytes:
    if name == "prices.csv":
        df = pd.read_csv(monitor.prices.final_path(symbol))
        df["status"] = df["status"].map(PRICE_STATUS_LABELS)
        df["source"] = df["source"].map(SOURCE_LABELS)
        return to_csv_bytes(df.rename(columns=PRICE_COLUMNS))
    if name == "eps.csv":
        df = pd.read_csv(monitor.earnings.paths(symbol)["quarterly"])
        return to_csv_bytes(df.rename(columns=QUARTERLY_COLUMNS))
    if name == "ttm.csv":
        df = pd.read_csv(monitor.earnings.paths(symbol)["ttm"])
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
