import json
from datetime import datetime

import pandas as pd
import pytest

from valuation_monitor.providers.sec import SecProvider, SecUserAgentMissing, headline, make_http_client, to_new_york

RESULTS_HTML = (
    b"<html><body><p>EX-99.1 Exhibit 99.1</p><h1>JD.com Announces Second Quarter 2026 Results</h1>" + b"<p>x</p>" * 20000 + b"</body></html>"
)
OTHER_HTML = b"<html><body><h1>Notice of Annual General Meeting</h1>" + b"<p>x</p>" * 20000 + b"</body></html>"


def test_headline():
    assert headline(RESULTS_HTML) == "JD.com Announces Second Quarter 2026 Results"
    assert headline(OTHER_HTML) is None


def test_acceptance_time_is_converted_to_new_york_time():
    assert to_new_york("2026-08-13T10:05:29.000Z") == pd.Timestamp("2026-08-13 06:05:29")  # 夏令时
    assert to_new_york("2026-01-13T21:05:29.000Z") == pd.Timestamp("2026-01-13 16:05:29")  # 冬令时


def test_missing_user_agent_is_reported(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(SecUserAgentMissing, match="SEC_USER_AGENT"):
        make_http_client()


SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["6-K", "6-K", "20-F"],
            "accessionNumber": ["0001193125-26-000001", "0001193125-26-000002", "0001193125-26-000003"],
            "acceptanceDateTime": ["2026-08-13T10:05:29.000Z", "2026-09-01T10:00:00.000Z", "2026-04-01T10:00:00.000Z"],
        },
        "files": [],
    }
}


def listing(*files):
    return json.dumps({"directory": {"item": [{"name": n, "size": str(s)} for n, s in files]}}).encode()


class FakeHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        self.requests.append(url)
        return SUBMISSIONS

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith("/000119312526000001/index.json"):
            return listing(("a6k.htm", 8000), ("ex991.htm", 300_000))
        if url.endswith("/000119312526000002/index.json"):
            return listing(("b6k.htm", 8000), ("ex991.htm", 20_000_000))  # 太大的附件不下载
        if url.endswith("/000119312526000001/ex991.htm"):
            return RESULTS_HTML
        raise AssertionError(url)


def test_provider_keeps_results_and_remembers_what_it_has_seen(tmp_path):
    http = FakeHttp()
    provider = SecProvider(http, tmp_path)
    found = provider.results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert [(f.title, f.published) for f in found] == [("JD.com Announces Second Quarter 2026 Results", pd.Timestamp("2026-08-13 06:05:29"))]
    assert found[0].content == RESULTS_HTML
    index = json.loads((tmp_path / "JD" / "index.json").read_text(encoding="utf-8"))
    assert [(e["accession"], e["results"]) for e in index] == [("0001193125-26-000001", True), ("0001193125-26-000002", False)]  # 20-F 不是 6-K

    n = len(http.requests)
    provider.results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert len(http.requests) == n + 1  # 只重新取了一次 submissions；看过的 6-K 不再检查、不再下载


def test_provider_works_offline_from_the_archive(tmp_path):
    SecProvider(FakeHttp(), tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
    offline = SecProvider(None, tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert [f.title for f in offline] == ["JD.com Announces Second Quarter 2026 Results"]
    assert SecProvider(None, tmp_path).results_announcements("JD", 1549802, datetime(2026, 9, 1)) == []
    with pytest.raises(SecUserAgentMissing):
        SecProvider(None, tmp_path / "empty").results_announcements("JD", 1549802, datetime(2026, 1, 1))
