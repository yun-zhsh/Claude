"""Build the download files: readable column names, and CSVs with a BOM so Excel opens them without garbled text."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd

from valuation_monitor.legacy import export_files
from valuation_monitor.service import PRICE_STATUS_LABELS, Monitor

PRICE_COLUMNS = {
    "date": "Date",
    "close": "Close (actual traded price)",
    "source": "Source used",
    "status": "Cross-check status",
    "tencent_close": "Tencent close",
    "yahoo_close_unadjusted": "Yahoo close (Yahoo's adjustments undone)",
    "yahoo_factor": "Yahoo adjustment factor",
}
SOURCE_LABELS = {"tencent": "Tencent", "yahoo": "Yahoo"}

# Per-period detail: every item has its value, first publication time, effective trading day, number of confirming filings and the largest deviation among them
SERIES_NAMES = {  # series -> (full name with unit, short name used in the other column names; must all differ)
    "eps": ("Basic EPS (IFRS/GAAP, per share; per ADS for ADRs)", "IFRS EPS"),
    "non_ifrs_eps": ("Basic EPS (Non-IFRS/Non-GAAP, per share; per ADS for ADRs)", "Non-IFRS EPS"),
    "revenue": ("Revenue (millions)", "Revenue"),
    "profit": ("Net profit attributable to owners (millions)", "Net profit"),
    "equity": ("Equity attributable to owners at period end (millions)", "Equity"),
    "wa_shares": ("Shares disclosed in the filing (millions; weighted average, period-end total shares for A-shares; millions of ADS for ADRs)", "Disclosed shares"),
}
PERIOD_COLUMNS = {"period": "Period (quarter or half year)", "currency": "Currency"}
for _key, (_name, _short) in SERIES_NAMES.items():
    PERIOD_COLUMNS |= {
        _key: _name,
        f"{_key}_published": f"{_short}: first published",
        f"{_key}_effective": f"{_short}: effective trading day",
        f"{_key}_sources": f"{_short}: confirming filings",
        f"{_key}_max_deviation": f"{_short}: largest deviation among filings",
    }
PERIOD_COLUMNS |= {"shares": "Implied shares (cross-check: net profit / basic EPS, millions)", "first_source": "Filing that first published this period"}

TTM_COLUMNS = {
    "report_date": "Period end",
    "published": "Published (local time of the exchange)",
    "available_date": "Available date (calendar day)",
    "effective_date": "Effective trading day",
    "provisional": "Preliminary data (1 = earnings call presentation or express report, replaced when the official report is out)",
    "revenue_ttm": "TTM revenue = sum of the periods' revenue (millions)",
    "shares": "Shares (millions; millions of ADS for ADRs): the weighted average shares disclosed in the filing, implied only if none is disclosed",
    "shares_source": "Share count source (reported / derived from the full-year figure / implied)",
    "shares_implied": "Cross-check: implied shares = latest net profit / latest basic EPS",
    "profit_latest": "Latest net profit attributable to owners (millions)",
    "eps_latest": "Latest basic EPS",
    "eps_ttm": "TTM EPS = sum of the periods' basic EPS",
    "eps_ttm_formula": "TTM EPS formula (* = obtained by subtracting cumulative figures)",
    "non_ifrs_eps_ttm": "TTM Non-IFRS/Non-GAAP EPS = sum of the periods",
    "non_ifrs_eps_ttm_formula": "TTM Non-IFRS/Non-GAAP EPS formula",
    "revenue_ttm_formula": "TTM revenue formula",
    "shares_formula": "Shares formula",
    "revenue_per_share_ttm": "Revenue per share (TTM) = TTM revenue / shares (reporting currency)",
    "revenue_per_share_formula": "Revenue per share formula",
    "equity": "Equity attributable to owners at period end (millions)",
    "book_value_per_share": "Book value per share = equity at period end / shares (reporting currency)",
    "book_value_per_share_formula": "Book value per share formula",
    "split_note": "Split adjustment note (figure in the filing -> adjusted)",
    "published_note": "Publication time note",
}
for _k in range(1, 5):  # the periods covered by the TTM (oldest to newest) and their values; they add up to the TTM
    TTM_COLUMNS |= {
        f"period_{_k}": f"TTM period {_k}",
        f"eps_{_k}": f"Period {_k}: basic EPS",
        f"non_ifrs_eps_{_k}": f"Period {_k}: Non-IFRS/Non-GAAP EPS",
        f"revenue_{_k}": f"Period {_k}: revenue (millions)",
    }

# file name in the URL -> file name saved on download (without the stock symbol)
FILES = {
    "prices.csv": "historical_prices.csv",
    "eps.csv": "historical_earnings.csv",
    "ttm.csv": "per_share_metrics.csv",
    "valuation.csv": "daily_valuation.csv",
    "raw.zip": "all_raw_data.zip",
}


def download_name(symbol: str, name: str) -> str:
    return f"{symbol}_{FILES[name]}"


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def drop_empty_series(df: pd.DataFrame) -> pd.DataFrame:
    """Drop all columns of the data series the company does not disclose (e.g. Sunny has no Non-IFRS EPS)."""
    empty = [key for key in SERIES_NAMES if key in df and df[key].isna().all()]
    suffixes = ("", "_published", "_effective", "_sources", "_max_deviation")
    return df.drop(columns=[f"{key}{suffix}" for key in empty for suffix in suffixes])


def build(monitor: Monitor, symbol: str, name: str, report=None) -> bytes:
    if name == "prices.csv":
        df = monitor.prices.final(symbol).rename_axis("date").reset_index().dropna(axis=1, how="all")  # only Hong Kong stocks have Tencent data
        df["date"] = df["date"].dt.strftime("%Y-%m-%d")
        df["status"] = df["status"].map(PRICE_STATUS_LABELS)
        df["source"] = df["source"].map(SOURCE_LABELS)
        return to_csv_bytes(df.rename(columns=PRICE_COLUMNS))
    if name == "eps.csv":
        df = drop_empty_series(monitor.earnings.periods(symbol))
        return to_csv_bytes(df.rename(columns=PERIOD_COLUMNS))
    if name == "ttm.csv":
        df = monitor.earnings.ttm(symbol).dropna(axis=1, how="all")
        return to_csv_bytes(df.rename(columns=TTM_COLUMNS))
    if name == "valuation.csv":
        return to_csv_bytes(valuation_frame(monitor, symbol, report))
    if name == "raw.zip":
        return raw_zip(monitor, symbol)
    raise KeyError(name)


def raw_zip(monitor: Monitor, symbol: str) -> bytes:
    """The saved data of this stock as CSV and JSON files (the layout of legacy.py); the original filings are left out, only their index is included."""
    files = export_files(monitor.db, symbol)
    index = monitor.data_dir / "filings" / symbol / "index.json"
    if index.is_file():
        files[f"filings/{symbol}/index.json"] = index.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in sorted(files.items()):
            zf.writestr(name, content)
    return buf.getvalue()


def valuation_frame(monitor: Monitor, symbol: str, report) -> pd.DataFrame:
    """The full calculation of every metric on every trading day: ratio = close / (per-share value x FX rate).

    "Earnings record" is the period of earnings used on that day (and when it was published); it matches the same row of
    the per-share metrics file, which has the formula of the per-share value.
    """
    data = report.data
    earnings = data.earnings
    out = pd.DataFrame({"Date": data.close.index, "Close (price currency)": data.close.to_numpy()})
    final = monitor.prices.final(symbol)
    if final is not None:
        out["Price source"] = final["source"].map(SOURCE_LABELS).reindex(data.close.index).to_numpy()
    out["FX rate (1 unit of reporting currency = ? price currency)"] = data.fx.to_numpy()
    for r in report.metrics:
        if r.error or not hasattr(r.metric, "detail"):
            continue
        d = r.metric.detail(data)
        fundamentals = data.fundamentals.reset_index(drop=True)
        rec = d["record"]
        known = rec.notna()
        idx = rec[known].astype(int)
        label = r.metric.label
        out[f"{label} | Earnings record (period)"] = pd.Series(
            [earnings.ttm_detail[i]["period_label"] for i in idx], index=idx.index
        ).reindex(d.index).to_numpy() if earnings and earnings.ttm_detail else None
        out[f"{label} | Earnings record published"] = pd.Series(fundamentals["published"].reindex(idx).to_numpy(), index=idx.index).reindex(d.index).to_numpy()
        out[f"{label} | Per-share value (reporting currency)"] = d["per_share"].to_numpy()
        out[f"{label} | Per-share value (price currency) = per-share value x FX rate"] = d["per_share_converted"].to_numpy()
        out[f"{label} | Ratio = close / per-share value (price currency)"] = d["ratio"].to_numpy()
    return out
