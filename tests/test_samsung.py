"""Samsung Electronics: parsing of the IR website pages, and the K-IFRS parsing verified with the English financial statements saved in the repo (data/filings/005930.KS)."""

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
    assert calls[(2015, 4)] == pd.Timestamp("2016-01-28 09:30")  # the fiscal Q4 is published in January of the next year
    assert calls[(2014, 3)] == pd.Timestamp("2014-10-30 10:00")  # only the date is given: 10:00 is assumed
    assert (2010, 2) not in calls  # entries with a wrong year on the page (date not 15-60 days after the quarter end) are dropped


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
    assert [f.published for f in found] == [pd.Timestamp("2026-10-29 10:00")]  # 2026Q2 has no call date; only 2026Q3 does
    assert found[0].attachments["bs"] == b"%PDF 2026_con_quarter03_bs.pdf"
    index = json.loads((tmp_path / "005930.KS" / "index.json").read_text(encoding="utf-8"))
    assert [(e["year"], e["quarter"]) for e in index] == [(2026, 3)]


pytestmark_saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved Samsung statements")


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
    assert p.values["eps"] == [10849, 737]  # matches the ordinary-share EPS in the earnings call material
    assert p.values["equity"] == [565064740, 424313255]  # total equity 579,309,676 - non-controlling interests 14,244,936


@pytestmark_saved
def test_annual_statement_is_cumulative_and_digit_group_artifacts_are_repaired():
    p = parsed(2025, 4)
    assert not p.quarterly and p.period == pd.Period("2025Q4")
    assert p.values["profit"] == [44260956, 33621363]  # written as "44, 260,956" in the PDF
    assert p.values["eps"] == [6605, 4950]


@pytestmark_saved
def test_label_variants_and_control_characters():
    assert parsed(2021, 1).values["profit"][0] == 7092786  # “Owners of the Company”
    assert parsed(2020, 3).values["equity"][0] == pytest.approx(267942140)  # control characters inside the text


@pytestmark_saved
def test_every_saved_statement_parses():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    assert len(index) >= 40
    for e in (e for e in index if not e.get("provisional")):
        p = parsed(e["year"], e["quarter"])
        assert p.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q")
        assert {"revenue", "profit", "eps", "equity"} <= p.values.keys()


@pytestmark_saved
def test_earnings_call_deck_is_used_until_the_statements_are_out():
    from valuation_monitor.kifrs import parse_kifrs_deck

    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    deck = next((e for e in index if e.get("provisional")), None)
    if deck is None:
        pytest.skip("the English statements of 3Q26 are out and have replaced the call material")
    prev = next(e for e in index if (e["year"], e["quarter"]) == (2026, 2))
    p = parse_kifrs_deck(
        "Samsung Electronics 3Q 2026 x", (FOLDER / deck["files"]["deck"]).read_bytes(), (FOLDER / prev["files"]["bs"]).read_bytes()
    )
    assert p.period == pd.Period("2026Q3") and p.quarterly
    assert p.values["eps"] == [11462, 1802, 10849]  # this quarter, same quarter last year, previous quarter
    assert p.values["revenue"] == [195_300_000, 86_100_000, 171_500_000]  # trillion KRW precise to 0.1
    assert p.values["equity"][0] == pytest.approx(614_111_600 * 565_064_740 / 579_309_676)  # total equity scaled by the previous quarter's attributable ratio


def test_deck_for_another_quarter_is_rejected():
    from valuation_monitor.kifrs import parse_kifrs_deck

    with pytest.raises(Exception):
        parse_kifrs_deck("Samsung Electronics 3Q 2026 x", b"not a pdf", b"")


class FakeHttpWithDeck(FakeHttp):
    statements = True

    def get_bytes(self, url, **kwargs):
        if "audited" in url:
            if not self.statements:
                return b"2026_con_quarter02_soi.pdf 2026_con_quarter02_bs.pdf"
            return b"2026_con_quarter02_soi.pdf 2026_con_quarter02_bs.pdf 2026_con_quarter03_soi.pdf 2026_con_quarter03_bs.pdf"
        if "earnings-release" in url:
            return b"2026_3Q_conference_eng.pdf"
        return super().get_bytes(url, **kwargs)


def test_provider_falls_back_to_deck_then_replaces_it(tmp_path):
    http = FakeHttpWithDeck()
    http.statements = False
    provider = SamsungIrProvider(http, tmp_path)
    # if the previous quarter's statements are also missing, no preliminary data is used (the equity estimate needs them)
    events = EVENTS_HTML + "<li>2Q26 Earnings Conference Call</li><li>July 30, 2026, 10:00 a.m. KST</li>"
    http_events = events.encode()
    original = http.get_bytes
    http.get_bytes = lambda url, **kw: http_events if "events" in url else original(url, **kw)
    found = provider.results_announcements("005930.KS", datetime(2026, 1, 1))
    assert [(f.title.split()[2], f.provisional) for f in found] == [("2Q", False), ("3Q", True)]
    assert found[1].attachments["previous_bs"] == b"%PDF 2026_con_quarter02_bs.pdf"

    http.statements = True  # English statements out: the preliminary call material is replaced, publication time unchanged
    found = provider.results_announcements("005930.KS", datetime(2026, 1, 1))
    assert [(f.title.split()[2], f.provisional, f.published) for f in found][-1] == ("3Q", False, pd.Timestamp("2026-10-29 10:00"))
    assert not list(tmp_path.glob("005930.KS/*conference_eng*"))


# Excerpt from the 4Q25 call material (pypdf extraction)
DECK_4Q25 = (
    "4Q 2025 Results & Financial Data Based on consolidated financial statements "
    "KRW trillion 4Q24 3Q25 % of sales 4Q25 % of sales 2024 % of sales 2025 % of sales "
    "Sales 75.8 86.1 100.0% 93.8 100.0% 300.9 100.0% 333.6 100.0% "
    "Profitattributable to owners of the parent 7.6 12.0 14.0% 19.3 20.6% 33.6 11.2% 44.3 13.3% "
    "EPS (KRW) ※ Common stock 1,116 1,802 2,909 4,950 6,605 4Q24 3Q25 4Q25 2024 2025 ROE 8% "
    "Shareholder equity 402,192.1 413,501.5 436,320.3 Capital stock 897.5"
)


@pytestmark_saved
def test_fourth_quarter_deck_is_read_as_full_year_figures(monkeypatch):
    from valuation_monitor import kifrs

    real = kifrs.read_text
    monkeypatch.setattr(kifrs, "read_text", lambda pdf: DECK_4Q25 if pdf == b"deck" else real(pdf))
    prev = next(e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if (e["year"], e["quarter"]) == (2025, 3))
    bs = (FOLDER / prev["files"]["bs"]).read_bytes()
    p = kifrs.parse_kifrs_deck("Samsung Electronics 4Q 2025 x", b"deck", bs)
    assert not p.quarterly and p.period == pd.Period("2025Q4")  # treated like the annual report as full-year cumulative figures; the single quarter is obtained as full year minus the first three quarters
    assert p.values["eps"] == [6605, 4950]  # full-year EPS is exact and matches the annual report
    assert p.values["revenue"] == [333_600_000, 300_900_000] and p.values["profit"] == [44_300_000, 33_600_000]
    assert p.values["equity"][0] == pytest.approx(436_320_300 * 0.975, rel=0.01)  # total equity scaled by the attributable ratio

    monkeypatch.setattr(kifrs, "read_text", lambda pdf: DECK_4Q25.replace("4Q 2025 Results", "4Q 2025 Result") if pdf == b"deck" else real(pdf))
    with pytest.raises(ValueError, match="layout has changed"):
        kifrs.parse_kifrs_deck("Samsung Electronics 4Q 2025 x", b"deck", bs)
