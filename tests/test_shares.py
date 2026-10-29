"""公告披露的加权平均股数的提取：腾讯、舜宇（PDF 文字）、京东（保存的新闻稿）、三星（附注页）。"""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.filings import WA_SHARES_RE, WA_SHARES_STMT_RE, Announcement
from valuation_monitor.kifrs import clean_text, current_and_prior, scale_to_millions

ROOT = Path(__file__).parent.parent / "data" / "filings"


def test_tencent_weighted_average_shares_row_in_both_wordings():
    old = "Weighted average number of ordinary shares in issue (million shares) 9,418 9,384 9,406 9,368 Basic EPS (RMB per share) 1.912"
    new = (
        "Weighted average number of ordinary shares in issue excluding shares held for share award scheme and treasury shares "
        "(million shares) 9,025 9,097 9,029 9,101 Basic EPS (RMB per share) 6.207"
    )
    assert WA_SHARES_RE.search(old + " ").group(1).split() == ["9,418", "9,384", "9,406", "9,368"]
    assert WA_SHARES_RE.search(new + " ").group(1).split() == ["9,025", "9,097", "9,029", "9,101"]
    annual = "Weighted average number of ordinary shares in issue (Note) (million shares) 9,231 9,158 Basic EPS"
    assert WA_SHARES_RE.search(annual + " ").group(1).split() == ["9,231", "9,158"]


def test_sunny_weighted_average_shares_row():
    text = "Weighted average number of ordinary shares for the purpose of basic earnings per share (Note) 1,076,196 1,086,161 Effect"
    m = WA_SHARES_STMT_RE.search(text)
    assert (m.group("a"), m.group("b")) == ("1,076,196", "1,086,161")  # 千股


def test_samsung_pdf_digit_group_artifacts_are_repaired():
    assert clean_text("shares outstanding 5,969,78 3 5,969,783 822,88 7") == "shares outstanding 5,969,783 5,969,783 822,887"
    assert clean_text("profit 44, 260,956 33,621,363") == "profit 44,260,956 33,621,363"
    assert clean_text("notes 18, 19 and 1,234 5") == "notes 18, 19 and 1,234 5"  # 不是被拆开的数字，保持原样


def test_samsung_note_columns_and_units():
    assert current_and_prior("5,767,030 5,791,848 5,881,154 5,903,847 ") == (5767030.0, 5881154.0)  # 三个月、累计各一对
    assert current_and_prior("5,889,607 5,969,672 ") == (5889607.0, 5969672.0)
    # 单位：千股（2023 年以前）或股（2024 年起），按净利润 ÷ EPS 的量级判断
    assert scale_to_millions([6_569_401.0, 1.0], 6569.2) == pytest.approx([6569.401, 0.001])
    assert scale_to_millions([6_792_669_250.0, 1.0], 6792.7)[0] == pytest.approx(6792.669)
    assert scale_to_millions([679.0, 1.0], 6792.7) is None  # 对不上任何单位时不硬用


def test_jd_press_releases_report_ads_weighted_average_shares():
    from valuation_monitor.press_release import parse_press_release

    folder = ROOT / "JD"
    if not (folder / "index.json").exists():
        pytest.skip("没有保存的京东公告")
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    for prefix, expected in (("2026-08-13", [1346.0, 1420.5]), ("2016-11-16", [1439.6, 1367.9])):  # 百万份 ADS
        entry = next(e for e in index if e["results"] and e["published"].startswith(prefix))
        p = parse_press_release((folder / entry["file"]).read_bytes())
        assert p.values["wa_shares"] == pytest.approx(expected, abs=0.06)
        assert p.values["wa_shares"][0] == pytest.approx(p.values["profit"][0] / p.values["eps"][0], rel=0.01)  # 与反推的接近
