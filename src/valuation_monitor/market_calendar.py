"""Trading hours: decide whether the closing price of the publication day already reflects a filing.

HK: full day closes at 16:00; Christmas Eve (24 Dec), New Year's Eve (31 Dec) and Lunar New Year's Eve, if trading days,
have a morning session only and close at 12:00.
KR: closes at 15:30 (Seoul time), no half days.
CN (A-shares): closes at 15:00 (Beijing time), no half days.
US (NYSE/Nasdaq): regular close 16:00 (New York time); the day after Thanksgiving, 3 July (Mon-Thu) and
Christmas Eve (Mon-Thu) close early at 13:00.

Which days are trading days is not decided here but by the price data that actually traded,
so weekends, public holidays and typhoon or rainstorm closures are excluded naturally.
Publication times are always local time of the exchange (no timezone).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Callable

import pandas as pd

FULL_DAY_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(12, 0)

# Lunar New Year (first day of the first lunar month, Gregorian date); the eve is the day before
LUNAR_NEW_YEAR = [
    "2010-02-14", "2011-02-03", "2012-01-23", "2013-02-10", "2014-01-31", "2015-02-19",
    "2016-02-08", "2017-01-28", "2018-02-16", "2019-02-05", "2020-01-25", "2021-02-12",
    "2022-02-01", "2023-01-22", "2024-02-10", "2025-01-29", "2026-02-17", "2027-02-06",
    "2028-01-26", "2029-02-13", "2030-02-03", "2031-01-23", "2032-02-11", "2033-01-31",
    "2034-02-19", "2035-02-08", "2036-01-28", "2037-02-15", "2038-02-04", "2039-01-24",
    "2040-02-12",
]
LUNAR_NEW_YEAR_EVES = {pd.Timestamp(d) - pd.Timedelta(days=1) for d in LUNAR_NEW_YEAR}


def _hk_half_day(day: pd.Timestamp) -> bool:
    if day.year > int(LUNAR_NEW_YEAR[-1][:4]):
        raise ValueError(f"The Lunar New Year table only goes up to {LUNAR_NEW_YEAR[-1][:4]}; please extend it")
    return (day.month, day.day) in ((12, 24), (12, 31)) or day in LUNAR_NEW_YEAR_EVES


def _thanksgiving(year: int) -> pd.Timestamp:
    """The fourth Thursday of November."""
    first = pd.Timestamp(year=year, month=11, day=1)
    return first + pd.Timedelta(days=(3 - first.weekday()) % 7 + 21)


def _us_half_day(day: pd.Timestamp) -> bool:
    if day == _thanksgiving(day.year) + pd.Timedelta(days=1):
        return True
    # 3 July and 24 December close early only on Monday-Thursday (on Friday/weekends the market is closed or follows other arrangements)
    return (day.month, day.day) in ((7, 3), (12, 24)) and day.weekday() < 4


@dataclass(frozen=True)
class Market:
    key: str
    timezone: str
    full_close: time
    half_close: time
    half_day: Callable[[pd.Timestamp], bool]
    cross_checks_prices: bool = False  # whether a second source of prices can be used to cross-check (only HK, via Tencent)

    def is_half_day(self, day: pd.Timestamp) -> bool:
        """Whether the market closes early on this day if it is open (does not decide whether it is open)."""
        return self.half_day(pd.Timestamp(day).normalize())

    def close_time(self, day: pd.Timestamp) -> time:
        return self.half_close if self.is_half_day(day) else self.full_close

    def available_date(self, published: pd.Timestamp) -> pd.Timestamp:
        """The earliest day whose closing price may already reflect this filing (calendar day, not necessarily a trading day).

        Published before the close (pre-market, intraday, lunch break): the same day; after the close: the next day.
        """
        day = pd.Timestamp(published).normalize()
        return day + pd.Timedelta(days=1) if pd.Timestamp(published).time() >= self.close_time(day) else day


HK = Market("HK", "Asia/Hong_Kong", FULL_DAY_CLOSE, HALF_DAY_CLOSE, _hk_half_day, cross_checks_prices=True)
US = Market("US", "America/New_York", FULL_DAY_CLOSE, time(13, 0), _us_half_day)
# Korea Exchange: closes at 15:30, no early-close half days (closure days are decided by the price data)
KR = Market("KR", "Asia/Seoul", time(15, 30), time(15, 30), lambda day: False)
# A-shares (SSE, SZSE): closes at 15:00, no half days (closure days are decided by the price data)
CN = Market("CN", "Asia/Shanghai", time(15, 0), time(15, 0), lambda day: False)
MARKETS = {m.key: m for m in (HK, US, KR, CN)}


def get_market(key: str) -> Market:
    try:
        return MARKETS[key]
    except KeyError:
        raise ValueError(f"Unknown market {key!r}; choose from: {', '.join(MARKETS)}") from None


# The functions below default to the HK market so existing calls keep working
def is_half_day(day: pd.Timestamp, market: Market = HK) -> bool:
    return market.is_half_day(day)


def close_time(day: pd.Timestamp, market: Market = HK) -> time:
    return market.close_time(day)


def available_date(published: pd.Timestamp, market: Market = HK) -> pd.Timestamp:
    return market.available_date(published)


def effective_trading_day(available: pd.Timestamp, trading_days: pd.DatetimeIndex) -> pd.Timestamp | None:
    """The first trading day on or after the available date.

    Returns None when the available date is before the start of the price data (cannot be decided) or after the last trading day (market not yet open).
    """
    available = pd.Timestamp(available).normalize()
    if len(trading_days) == 0 or available < trading_days[0]:
        return None
    later = trading_days[trading_days >= available]
    return later[0] if len(later) else None
