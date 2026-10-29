"""三星电子：IR 网站页面解析，以及用仓库里保存的英文财务报表原文（data/filings/005930.KS）验证 K-IFRS 解析。"""

import json
from datetime import datetime
from functools import cache
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.kifrs import parse_kifrs
from valuation_monitor.providers.samsung import SamsungIrProvider, discover, parse_calls

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "005930.KS"

EVENTS_HTML = """<ul>
<li>3Q26 Earnings Conference Call</li><li>October 29, 2026, 10:00 a.m. KST</li>
<li>4Q15 Earnings Conference Call</li><li>January 28, 2016, 9:30 a.m. KST</li>
<li>3Q FY2014 Earnings Conference Call</li><li>October 30, 2014</li>
<li>2Q FY2010 Earnings Conference Call</li><li>July 30, 2009</li>
<li>CLSA, Investor's Forum 2026</li><li>September 21 ~ 22, 2026</li>
</ul>"""


def test_parse_calls():
    calls = parse_calls(EVENTS_HTML)
    assert calls[(2026, 3)] == pd.Timestamp("2026-10-29 10:00")
    assert calls[(2015, 4)] == pd.Timestamp("2016-01-28 09:30")  # 财年第四季度在次年 1 月发布
    assert calls[(2014, 3)] == pd.Timestamp("2014-10-30 10:00")  # 只写了日期：按 10:00
    assert (2010, 2) not in calls  # 页面上年份写错的条目（日期不在季度结束后 15～60 天内）被丢弃


def test_discover_prefers_corrected_versions():
    html = "2019_con_quarter02_soi_new.pdf 2019_con_quarter02_bs_new.pdf 2022_con_quarter04_soi.pdf 2022_con_quarter04_soi_1.pdf 2022_con_quarter04_cf.pdf"
    found = discover([html])
    assert found[(2019, 2)]["soi"].endswith("2019_con_quarter02_soi_new.pdf")
    assert found[(2022, 4)]["soi"].endswith("2022_con_quarter04_soi_1.pdf") and "cf" not in found[(2022, 4)]


class FakeHttp:
    def __init__(self):
        self.requests = []

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if "events" in url:
            return EVENTS_HTML.encode()
        if "audited" in url:
            return b"2026_con_quarter02_soi.pdf 2026_con_quarter02_bs.pdf 2026_con_quarter03_soi.pdf 2026_con_quarter03_bs.pdf"
        if "earnings-release" in url:
            return b""
        return b"%PDF " + url.rsplit("/", 1)[-1].encode()


def test_provider_uses_call_time_and_skips_quarters_without_a_call(tmp_path):
    provider = SamsungIrProvider(FakeHttp(), tmp_path)
    found = provider.results_announcements("005930.KS", datetime(2026, 1, 1))
    assert [f.published for f in found] == [pd.Timestamp("2026-10-29 10:00")]  # 2026Q2 没有发布会日期，2026Q3 的才有
    assert found[0].attachments["bs"] == b"%PDF 2026_con_quarter03_bs.pdf"
    index = json.loads((tmp_path / "005930.KS" / "index.json").read_text(encoding="utf-8"))
    assert [(e["year"], e["quarter"]) for e in index] == [(2026, 3)]


pytestmark_saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="没有保存的三星财报")


@cache
def parsed(year: int, quarter: int):
    entry = next(e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if (e["year"], e["quarter"]) == (year, quarter))
    return parse_kifrs(f"Samsung Electronics {quarter}Q {year}", (FOLDER / entry["files"]["soi"]).read_bytes(), (FOLDER / entry["files"]["bs"]).read_bytes())


@pytestmark_saved
def test_interim_statement_takes_three_month_columns():
    p = parsed(2026, 2)
    assert p.period == pd.Period("2026Q2") and p.quarterly and p.currency == "KRW"
    assert p.values["revenue"] == [171499470, 74566317]
    assert p.values["profit"] == [71269468, 4934034]
    assert p.values["eps"] == [10849, 737]  # 与业绩发布会材料里的普通股 EPS 一致
    assert p.values["equity"] == [565064740, 424313255]  # 合计权益 579,309,676 − 非控股权益 14,244,936


@pytestmark_saved
def test_annual_statement_is_cumulative_and_digit_group_artifacts_are_repaired():
    p = parsed(2025, 4)
    assert not p.quarterly and p.period == pd.Period("2025Q4")
    assert p.values["profit"] == [44260956, 33621363]  # PDF 里写成“44, 260,956”
    assert p.values["eps"] == [6605, 4950]


@pytestmark_saved
def test_label_variants_and_control_characters():
    assert parsed(2021, 1).values["profit"][0] == 7092786  # “Owners of the Company”
    assert parsed(2020, 3).values["equity"][0] == pytest.approx(267942140)  # 文字里夹着控制字符


@pytestmark_saved
def test_every_saved_statement_parses():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    assert len(index) >= 40
    for e in index:
        p = parsed(e["year"], e["quarter"])
        assert p.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q")
        assert {"revenue", "profit", "eps", "equity"} <= p.values.keys()
