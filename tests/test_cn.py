"""A 股（海康威视）：巨潮资讯网的公告列表、发布时间判断，以及用保存的中文报告原文验证解析。"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.cn_report import clean_text, parse_cn_express, parse_cn_report
from valuation_monitor.config import parse_config
from valuation_monitor.market_calendar import CN
from valuation_monitor.providers.cninfo import CninfoProvider, classify, published_time

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "002415.SZ"
saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="没有保存的海康威视公告")


def test_classify_titles():
    assert classify("2026年第三季度报告") == ("report", 2026, 3)
    assert classify("2025年三季度报告") == ("report", 2025, 3)
    assert classify("2026年一季度报告") == ("report", 2026, 1)
    assert classify("2014年第一季度报告全文") == ("report", 2014, 1)
    assert classify("2015年第一季度报告全文（更新后）") == ("report", 2015, 1)
    assert classify("2025年半年度报告") == ("report", 2025, 2)
    assert classify("2025年年度报告") == ("report", 2025, 4)
    assert classify("2025年度业绩快报") == ("express", 2025, 4)
    for skipped in ("2025年年度报告摘要", "2025年年度报告（英文版）", "2025年度业绩快报（英文版）", "2025年环境、社会及治理报告", "2025年第三季度报告更正公告"):
        assert classify(skipped) is None


def test_published_time_prefers_the_upload_time_but_only_after_the_2020_migration():
    listed = pd.Timestamp("2026-10-17 00:00")  # 周五 16:00 盘后发布，巨潮记在次日 00:00
    assert published_time(listed, "Fri, 16 Oct 2026 08:00:19 GMT")[0] == pd.Timestamp("2026-10-16 16:00:19")
    # 2020 年 9 月迁移后的文件，上传时间是迁移时间，不能用：只用公告日期
    old = pd.Timestamp("2018-10-20 00:00")
    assert published_time(old, "Sat, 26 Sep 2020 08:25:00 GMT")[0] == old
    assert published_time(old, None)[0] == old
    # 上传时间与公告日期对不上（相差超过一天）时也不用
    assert published_time(listed, "Mon, 05 Oct 2026 08:00:00 GMT")[0] == listed


def test_a_share_market_closes_at_1500():
    assert CN.available_date(pd.Timestamp("2026-10-16 16:00")) == pd.Timestamp("2026-10-17")  # 周五盘后：下个交易日（行情里的周一）
    assert CN.available_date(pd.Timestamp("2026-10-17 00:00")) == pd.Timestamp("2026-10-17")  # 只有日期的按开盘前
    assert CN.available_date(pd.Timestamp("2026-10-16 14:59")) == pd.Timestamp("2026-10-16")


def test_config_for_a_share():
    s = parse_config({"stocks": [{"symbol": "002415.SZ", "name": "海康威视", "metrics": ["pe"]}]}).stocks[0]
    assert (s.market, s.filing_format, s.cik) == ("CN", "cn_report", None) and "巨潮" in s.filing_source and not s.cross_checks_prices
    with pytest.raises(ValueError, match="不匹配"):
        parse_config({"stocks": [{"symbol": "002415.SZ", "filing_format": "highlights"}]})


def test_pdf_digit_artifacts_are_repaired():
    assert clean_text("股本 9,335,806,1 14.00 9,343,417,190.00") == "股本 9,335,806,114.00 9,343,417,190.00"
    assert clean_text("净利润 10,965,641,158.1 1 29.94%") == "净利润 10,965,641,158.11 29.94%"
    assert clean_text("归属于上市 公司股东") == "归属于上市公司股东"


class FakeHttp:
    """假的巨潮资讯：一份第三季度报告（盘后发布），一份年度业绩快报，之后又发布了年报。"""

    def __init__(self):
        self.lists = {}
        self.downloads = []

    def post_json(self, url, data, retries=5):
        if "topSearch" in url:
            return [{"code": "002415", "orgId": "9900012688"}]
        start = data["seDate"][:4]
        rows = [r for r in self.lists.get(data["searchkey"], []) if r["announcementTime"] is not None and (data["searchkey"] or start in r["_date"])]
        return {"announcements": rows, "hasMore": False}

    def get_response(self, url, retries=5):
        self.downloads.append(url)

        class Resp:
            content = b"%PDF fake"
            headers = {"Last-Modified": "Fri, 16 Oct 2026 08:00:19 GMT"}

        return Resp()


def row(aid, title, date_ms, date):
    return {"announcementId": aid, "announcementTitle": title, "announcementTime": date_ms, "adjunctUrl": f"finalpage/{date}/{aid}.PDF", "_date": date}


def test_provider_classifies_dates_and_replaces_the_express_report(tmp_path, monkeypatch):
    monkeypatch.setattr("valuation_monitor.providers.cninfo.excerpt_pages", lambda pdf: pdf)
    http = FakeHttp()
    ms = lambda s: int(pd.Timestamp(s, tz="Asia/Shanghai").timestamp() * 1000)  # noqa: E731
    http.lists[""] = [
        row("1", "2026年第三季度报告", ms("2026-10-17"), "2026-10-17"),
        row("2", "2026年第三季度报告（英文版）", ms("2026-10-17"), "2026-10-17"),
        row("3", "2026年第三季度报告摘要", ms("2026-10-17"), "2026-10-17"),
    ]
    http.lists["业绩快报"] = [row("4", "2025年度业绩快报", ms("2026-01-21"), "2026-01-21")]
    provider = CninfoProvider(http, tmp_path)
    found = provider.results_announcements("002415.SZ", datetime(2025, 12, 1))
    assert [(f.title, f.provisional) for f in found] == [("2025年度业绩快报", True), ("2026年第三季度报告", False)]
    assert found[1].published == pd.Timestamp("2026-10-16 16:00:19")  # 用 PDF 上传时间，不是列表里的次日 00:00
    index = json.loads((tmp_path / "002415.SZ" / "index.json").read_text(encoding="utf-8"))
    assert {e["id"] for e in index} == {"1", "4"}  # 英文版、摘要不下载

    n = len(http.downloads)
    provider.results_announcements("002415.SZ", datetime(2025, 12, 1))
    assert len(http.downloads) == n  # 已保存的不再下载

    # 年报发布后：快报被取代，年报的发布时间沿用快报那天
    http.lists[""].append(row("5", "2025年年度报告", ms("2026-04-18"), "2026-04-18"))
    found = provider.results_announcements("002415.SZ", datetime(2025, 12, 1))
    annual = next(f for f in found if "年度报告" in f.title)
    assert not annual.provisional and annual.published == pd.Timestamp("2026-01-21 00:00")  # 年报的数字第一次公开是在快报那天
    assert "沿用业绩快报" in annual.note and "2026-04-18" in annual.note  # 说明数字是年报的、发布时间是快报的
    assert [f.title for f in found] == ["2025年年度报告", "2026年第三季度报告"]


@saved
def test_saved_reports_parse_and_match_the_key_table():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    assert len(index) >= 50
    for e in index:
        content = (FOLDER / e["file"]).read_bytes()
        p = parse_cn_express(e["title"], content) if e["kind"] == "express" else parse_cn_report(e["title"], content)
        assert p.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q")
        assert {"revenue", "profit", "eps", "equity", "wa_shares"} <= p.values.keys()


def parsed(title_prefix, kind="report"):
    e = next(e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e["kind"] == kind and e["title"].startswith(title_prefix))
    content = (FOLDER / e["file"]).read_bytes()
    return (parse_cn_express if kind == "express" else parse_cn_report)(e["title"], content)


@saved
def test_third_quarter_uses_the_single_quarter_column():
    p = parsed("2026年三季度报告")
    assert p.quarterly and p.period == pd.Period("2026Q3")
    assert p.values["revenue"] == pytest.approx([27221.79484181]) and p.values["profit"] == pytest.approx([5015.68079576])
    assert p.values["eps"] == [0.547]  # 单季，不是前三季度累计的 1.409
    assert p.values["wa_shares"] == pytest.approx([9164.87155])  # 期末总股本，百万股
    assert p.values["equity"] == pytest.approx([84274.81359135, 83348.18588562])


@saved
def test_annual_and_half_year_reports_are_cumulative_with_prior_year_columns():
    p = parsed("2025年年度报告")
    assert not p.quarterly and p.period == pd.Period("2025Q4")
    assert p.values["eps"] == [1.546, 1.297] and p.values["profit"][0] == pytest.approx(14195.0, abs=1)
    assert parsed("2026年半年度报告").values["eps"] == [0.862, 0.615]


@saved
def test_express_report_is_provisional_cumulative_data():
    p = parsed("2025年度业绩快报", "express")
    assert not p.quarterly and p.period == pd.Period("2025Q4")
    assert p.values["eps"] == [1.545, 1.297] and p.values["wa_shares"] == pytest.approx([9164.87155])
