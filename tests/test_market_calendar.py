import pandas as pd
import pytest

from valuation_monitor.market_calendar import available_date, effective_trading_day, is_half_day


@pytest.mark.parametrize(
    "published,expected",
    [
        ("2026-08-12 16:31", "2026-08-13"),  # 盘后
        ("2026-08-12 16:00", "2026-08-13"),  # 收盘那一刻
        ("2026-08-12 07:30", "2026-08-12"),  # 盘前
        ("2026-08-12 12:15", "2026-08-12"),  # 午休
        ("2025-12-24 11:30", "2025-12-24"),  # 半日市，收盘前
        ("2025-12-24 12:30", "2025-12-25"),  # 半日市，收盘后
        ("2026-02-16 13:00", "2026-02-17"),  # 农历除夕，半日市收盘后
        ("2026-08-15 10:00", "2026-08-15"),  # 周六发布：可用日期是当天，生效日由交易日决定
    ],
)
def test_available_date(published, expected):
    assert available_date(pd.Timestamp(published)) == pd.Timestamp(expected)


def test_half_days():
    assert is_half_day(pd.Timestamp("2024-12-31"))
    assert is_half_day(pd.Timestamp("2024-02-09"))  # 农历除夕
    assert not is_half_day(pd.Timestamp("2024-02-08"))
    with pytest.raises(ValueError):
        is_half_day(pd.Timestamp("2041-01-01"))


def test_effective_trading_day_skips_closed_days():
    # 2024-05-14 周二 16:30 发布，05-15 佛诞休市
    days = pd.to_datetime(["2024-05-13", "2024-05-14", "2024-05-16", "2024-05-17"])
    available = available_date(pd.Timestamp("2024-05-14 16:30"))
    assert effective_trading_day(available, days) == pd.Timestamp("2024-05-16")
    assert effective_trading_day(pd.Timestamp("2024-05-18"), days) is None  # 尚未开市
    assert effective_trading_day(pd.Timestamp("2024-05-01"), days) is None  # 早于行情数据


def test_us_calendar_early_closes_and_after_close():
    from valuation_monitor.market_calendar import US

    assert US.is_half_day(pd.Timestamp("2025-11-28"))  # 感恩节次日
    assert US.is_half_day(pd.Timestamp("2024-07-03")) and not US.is_half_day(pd.Timestamp("2025-07-03") + pd.Timedelta(days=1))
    assert US.is_half_day(pd.Timestamp("2024-12-24")) and not US.is_half_day(pd.Timestamp("2026-12-24") + pd.Timedelta(days=1))
    assert not US.is_half_day(pd.Timestamp("2025-11-27"))
    # 盘前 / 盘中 / 收盘后
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
