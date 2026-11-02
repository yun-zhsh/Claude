import pandas as pd
import pytest

from valuation_monitor.market_calendar import available_date, effective_trading_day, is_half_day


@pytest.mark.parametrize(
    "published,expected",
    [
        ("2026-08-12 16:31", "2026-08-13"),  # after the close
        ("2026-08-12 16:00", "2026-08-13"),  # the moment of the close
        ("2026-08-12 07:30", "2026-08-12"),  # before the open
        ("2026-08-12 12:15", "2026-08-12"),  # lunch break
        ("2025-12-24 11:30", "2025-12-24"),  # half day, before the close
        ("2025-12-24 12:30", "2025-12-25"),  # half day, after the close
        ("2026-02-16 13:00", "2026-02-17"),  # Lunar New Year's Eve, half day, after the close
        ("2026-08-15 10:00", "2026-08-15"),  # published on a Saturday: the available date is that day, the effective day is decided by the trading days
    ],
)
def test_available_date(published, expected):
    assert available_date(pd.Timestamp(published)) == pd.Timestamp(expected)


def test_half_days():
    assert is_half_day(pd.Timestamp("2024-12-31"))
    assert is_half_day(pd.Timestamp("2024-02-09"))  # Lunar New Year's Eve
    assert not is_half_day(pd.Timestamp("2024-02-08"))
    with pytest.raises(ValueError):
        is_half_day(pd.Timestamp("2041-01-01"))


def test_effective_trading_day_skips_closed_days():
    # published Tuesday 2024-05-14 16:30; 05-15 is closed for Buddha's Birthday
    days = pd.to_datetime(["2024-05-13", "2024-05-14", "2024-05-16", "2024-05-17"])
    available = available_date(pd.Timestamp("2024-05-14 16:30"))
    assert effective_trading_day(available, days) == pd.Timestamp("2024-05-16")
    assert effective_trading_day(pd.Timestamp("2024-05-18"), days) is None  # market not yet open
    assert effective_trading_day(pd.Timestamp("2024-05-01"), days) is None  # before the start of the price data


def test_us_calendar_early_closes_and_after_close():
    from valuation_monitor.market_calendar import US

    assert US.is_half_day(pd.Timestamp("2025-11-28"))  # the day after Thanksgiving
    assert US.is_half_day(pd.Timestamp("2024-07-03")) and not US.is_half_day(pd.Timestamp("2025-07-03") + pd.Timedelta(days=1))
    assert US.is_half_day(pd.Timestamp("2024-12-24")) and not US.is_half_day(pd.Timestamp("2026-12-24") + pd.Timedelta(days=1))
    assert not US.is_half_day(pd.Timestamp("2025-11-27"))
    # before the open / intraday / after the close
    assert US.available_date(pd.Timestamp("2026-08-13 06:05")) == pd.Timestamp("2026-08-13")
    assert US.available_date(pd.Timestamp("2026-08-13 15:59")) == pd.Timestamp("2026-08-13")
    assert US.available_date(pd.Timestamp("2026-08-13 16:00")) == pd.Timestamp("2026-08-14")
    assert US.available_date(pd.Timestamp("2025-11-28 13:30")) == pd.Timestamp("2025-11-29")


def test_korean_market_closes_at_1530_without_half_days():
    from valuation_monitor.market_calendar import KR

    assert KR.available_date(pd.Timestamp("2026-10-29 10:00")) == pd.Timestamp("2026-10-29")
    assert KR.available_date(pd.Timestamp("2026-10-29 15:29")) == pd.Timestamp("2026-10-29")
    assert KR.available_date(pd.Timestamp("2026-10-29 15:30")) == pd.Timestamp("2026-10-30")
    assert not KR.is_half_day(pd.Timestamp("2025-12-24"))
