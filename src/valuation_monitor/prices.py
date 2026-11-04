"""Combine the daily data of several sources and decide the final closing price (actual traded price) of each day.

Sources:
  - Tencent: unadjusted actual traded prices.
  - Yahoo: close is adjusted for ex-rights events (splits, distributions in kind); the factor is constant
    over time segments and jumps around the ex-rights date.

Method:
  1. Rows with zero volume are invalid (Yahoo generates fake zero-volume rows on typhoon closure days and half days).
  2. Estimate the daily Yahoo adjustment factor from the ratio of the two sources: a day whose ratio equals that of
     the previous or next trading day takes it; a day that disagrees with both is an outlier and keeps the previous valid
     factor. This also gives the correct segment factor on the ex-rights date itself.
  3. Undo the adjustment on the Yahoo price and compare with Tencent; a difference of at most 0.2% counts as agreement.
  4. The final price is the Tencent price; the unadjusted Yahoo price is used only on days Tencent is missing. On
     disagreement the Tencent price is still used (Yahoo is known to be wrong on half days etc.) but it is flagged in the result for manual review.
"""

from __future__ import annotations

import pandas as pd

AGREE_TOLERANCE = 0.002

FINAL_COLUMNS = ["close", "source", "status", "tencent_close", "yahoo_close_unadjusted", "yahoo_factor"]


def valid_close(bars: pd.DataFrame) -> pd.Series:
    if bars.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))  # an empty series still needs a date index: with pandas 2 the combined index would be of objects
    return bars.loc[bars["volume"] > 0, "close"]


def consolidate(tencent: pd.DataFrame, yahoo: pd.DataFrame) -> pd.DataFrame:
    t = valid_close(tencent)
    y = valid_close(yahoo)
    days = t.index.union(y.index)
    t, y = t.reindex(days), y.reindex(days)

    ratio = (y / t).dropna()
    consistent = ((ratio / ratio.shift(1) - 1).abs() <= AGREE_TOLERANCE) | (
        (ratio / ratio.shift(-1) - 1).abs() <= AGREE_TOLERANCE
    )
    factor = ratio[consistent].reindex(days).ffill().bfill()
    if factor.isna().all():  # too few overlapping days to estimate
        factor = pd.Series(ratio.median() if len(ratio) else 1.0, index=days)
    y_unadjusted = y / factor

    agree = (t / y_unadjusted - 1).abs() <= AGREE_TOLERANCE
    status = pd.Series("tencent_only", index=days)
    status[y.notna() & t.isna()] = "yahoo_only"
    status[t.notna() & y.notna()] = "mismatch"
    status[t.notna() & y.notna() & agree] = "agree"

    out = pd.DataFrame(
        {
            "close": t.fillna(y_unadjusted),
            "source": pd.Series("tencent", index=days).where(t.notna(), "yahoo"),
            "status": status,
            "tencent_close": t,
            "yahoo_close_unadjusted": y_unadjusted.round(3),
            "yahoo_factor": factor.round(6),
        },
        columns=FINAL_COLUMNS,
    )
    out["close"] = out["close"].round(3)
    return out
