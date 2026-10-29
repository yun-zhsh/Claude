import pandas as pd
import pytest

from valuation_monitor.prices import consolidate


def frame(closes, volumes=None):
    idx = pd.date_range("2022-01-03", periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes, "volume": volumes or [100] * len(closes)}, index=idx, dtype=float)


def test_consolidate_handles_stepwise_yahoo_adjustment():
    raw = [100.0 + i for i in range(30)]
    # Yahoo 在第 15 天之前把价格调低 5%（如实物分派的除权）
    yahoo = [p * (0.95 if i < 15 else 1.0) for i, p in enumerate(raw)]
    out = consolidate(frame(raw), frame(yahoo))
    assert (out["status"] == "agree").all()
    assert out["yahoo_factor"].iloc[:15].tolist() == pytest.approx([0.95] * 15)
    assert out["yahoo_factor"].iloc[15:].tolist() == pytest.approx([1.0] * 15)
    assert out["close"].tolist() == raw


def test_consolidate_flags_mismatch_and_ignores_zero_volume():
    raw = [100.0] * 20
    yahoo = list(raw)
    yahoo[5] = 103.0  # 某天 Yahoo 数据有误
    yahoo_vol = [100] * 20
    yahoo_vol[8] = 0  # 停市日的假数据
    tencent = frame(raw).drop(frame(raw).index[8])  # 腾讯当天没有数据
    out = consolidate(tencent, frame(yahoo, yahoo_vol))

    day5, day8 = out.index[5], frame(raw).index[8]
    assert out.loc[day5, "status"] == "mismatch"
    assert out.loc[day5, "close"] == 100.0  # 不一致时采用腾讯价格
    assert day8 not in out.index  # 双方都没有有效数据
    assert (out.drop(day5)["status"] == "agree").all()


def test_consolidate_fills_missing_tencent_day_from_yahoo():
    raw = [100.0 + i for i in range(20)]
    yahoo = [p * 0.9 for p in raw]
    tencent = frame(raw).drop(frame(raw).index[10])
    out = consolidate(tencent, frame(yahoo))
    day = frame(raw).index[10]
    assert out.loc[day, "status"] == "yahoo_only"
    assert out.loc[day, "source"] == "yahoo"
    assert out.loc[day, "close"] == pytest.approx(110.0)


def test_consolidate_without_yahoo():
    out = consolidate(frame([100.0, 101.0]), pd.DataFrame(columns=["close", "volume"]))
    assert out["status"].tolist() == ["tencent_only"] * 2
    assert out["close"].tolist() == [100.0, 101.0]


def test_consolidate_ex_date_next_to_missing_day():
    raw = [100.0] * 20
    yahoo = [p * (0.95 if i < 10 else 1.0) for i, p in enumerate(raw)]
    yahoo_vol = [100] * 20
    yahoo_vol[9] = 0  # 除权日前一天 Yahoo 无有效数据
    out = consolidate(frame(raw), frame(yahoo, yahoo_vol))
    assert out["status"].value_counts().to_dict() == {"agree": 19, "tencent_only": 1}
    assert out["yahoo_factor"].iloc[10] == 1.0
