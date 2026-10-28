"""综合多个来源的日线数据，判定每天最终采用的收盘价（实际成交价）。

来源：
  - 腾讯证券：未复权的实际成交价。
  - Yahoo：close 经过除权调整（拆股、实物分派），系数随时间分段不变，
    在除权日前后跳变。

判定方法：
  1. 成交量为 0 的数据视为无效（Yahoo 会在台风停市日、半日市生成成交量为 0 的假数据）。
  2. 用两者的比值估计 Yahoo 每天的调整系数：某天的比值与前一个或后一个交易日的比值
     一致，就采用它；与前后都不一致的是异常日，沿用前一个有效系数。这样除权日当天
     也能得到正确的分段系数。
  3. 把 Yahoo 价格还原成未调整价格，与腾讯价格比较，相差不超过 0.2% 视为一致。
  4. 最终采用腾讯价格；腾讯缺失的日子才用还原后的 Yahoo 价格。两者不一致时仍用
     腾讯价格（已知 Yahoo 在半日市等情况下有误），但会在结果中标出，方便人工核对。
"""

from __future__ import annotations

import pandas as pd

AGREE_TOLERANCE = 0.002

FINAL_COLUMNS = ["close", "source", "status", "tencent_close", "yahoo_close_unadjusted", "yahoo_factor"]


def valid_close(bars: pd.DataFrame) -> pd.Series:
    if bars.empty:
        return pd.Series(dtype=float)
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
    if factor.isna().all():  # 重叠的日子太少，无法估计
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
