"""港股交易时间：判断公告发布时当天的收盘价是否已经反映了这份公告。

港交所全日市 16:00 收盘；平安夜（12 月 24 日）、除夕（12 月 31 日）、农历除夕如果是交易日，
只开上午市，12:00 收盘。哪些日子是交易日不在这里判断，而是以实际有成交的行情数据为准，
这样周末、公众假期、台风或暴雨停市都会自然排除。
"""

from __future__ import annotations

from datetime import time

import pandas as pd

FULL_DAY_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(12, 0)

# 农历正月初一（公历），除夕是前一天
LUNAR_NEW_YEAR = [
    "2010-02-14", "2011-02-03", "2012-01-23", "2013-02-10", "2014-01-31", "2015-02-19",
    "2016-02-08", "2017-01-28", "2018-02-16", "2019-02-05", "2020-01-25", "2021-02-12",
    "2022-02-01", "2023-01-22", "2024-02-10", "2025-01-29", "2026-02-17", "2027-02-06",
    "2028-01-26", "2029-02-13", "2030-02-03", "2031-01-23", "2032-02-11", "2033-01-31",
    "2034-02-19", "2035-02-08", "2036-01-28", "2037-02-15", "2038-02-04", "2039-01-24",
    "2040-02-12",
]
LUNAR_NEW_YEAR_EVES = {pd.Timestamp(d) - pd.Timedelta(days=1) for d in LUNAR_NEW_YEAR}


def is_half_day(day: pd.Timestamp) -> bool:
    """如果这天开市，是否只开上午市（不判断这天是否开市）。"""
    day = pd.Timestamp(day).normalize()
    if day.year > int(LUNAR_NEW_YEAR[-1][:4]):
        raise ValueError(f"农历新年日期表只到 {LUNAR_NEW_YEAR[-1][:4]} 年，请补充")
    return (day.month, day.day) in ((12, 24), (12, 31)) or day in LUNAR_NEW_YEAR_EVES


def close_time(day: pd.Timestamp) -> time:
    return HALF_DAY_CLOSE if is_half_day(day) else FULL_DAY_CLOSE


def available_date(published: pd.Timestamp) -> pd.Timestamp:
    """最早哪一天的收盘价可能已经反映了这份公告（自然日，不一定是交易日）。

    当天收盘前发布（盘前、盘中、午休）：当天；收盘后发布：下一天。
    """
    day = pd.Timestamp(published).normalize()
    return day + pd.Timedelta(days=1) if published.time() >= close_time(day) else day


def effective_trading_day(available: pd.Timestamp, trading_days: pd.DatetimeIndex) -> pd.Timestamp | None:
    """可用日期当天或之后的第一个交易日。

    返回 None 的情况：可用日期早于行情数据的起点（无法确定），或晚于最后一个交易日（尚未开市）。
    """
    available = pd.Timestamp(available).normalize()
    if len(trading_days) == 0 or available < trading_days[0]:
        return None
    later = trading_days[trading_days >= available]
    return later[0] if len(later) else None
