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
    assert to_new_york("2026-08-13T10:05:29.000Z") == pd.Timestamp("2026-08-13 06:05:29")  # daylight saving time
    assert to_new_york("2026-01-13T21:05:29.000Z") == pd.Timestamp("2026-01-13 16:05:29")  # standard time


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
            return listing(("b6k.htm", 8000), ("ex991.htm", 20_000_000))  # exhibits that are too large are not downloaded
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
    assert [(e["accession"], e["results"]) for e in index] == [("0001193125-26-000001", True), ("0001193125-26-000002", False)]  # the 20-F is not a 6-K

    n = len(http.requests)
    provider.results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert len(http.requests) == n + 1  # submissions fetched only once more; 6-Ks already seen are not checked or downloaded again


def test_provider_works_offline_from_the_archive(tmp_path):
    SecProvider(FakeHttp(), tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
    offline = SecProvider(None, tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert [f.title for f in offline] == ["JD.com Announces Second Quarter 2026 Results"]
    assert SecProvider(None, tmp_path).results_announcements("JD", 1549802, datetime(2026, 9, 1)) == []
    with pytest.raises(SecUserAgentMissing):
        SecProvider(None, tmp_path / "empty").results_announcements("JD", 1549802, datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("EX-99.1 Exhibit 99.1 Meta Reports Third Quarter 2026 Results", "Meta Reports Third Quarter 2026 Results"),
        ("Cognizant Reports Fourth Quarter and Full Year 2025 Results", "Cognizant Reports Fourth Quarter and Full Year 2025 Results"),
        ("ASML reports EUR 2.6 billion sales at 43.0% gross margin in Q2 Stronger Logic compensates for Memory weakness VELDHOVEN, July 17, 2019 - today ASML Holding N.V. (ASML) publishes its 2019 second-quarter results.", None),
        ("Notice of Annual General Meeting", None),
    ],
)
def test_headline_for_other_companies(text, expected):
    html = f"<html><body><p>{text}</p>".encode() + b"<p>x</p>" * 5000 + b"</body></html>"
    found = headline(html)
    if expected is None and "ASML" not in text:
        assert found is None
    elif expected is None:
        assert found is not None and "second-quarter results" in found  # "today ... publishes its 2019 second-quarter results" counts as a results announcement
    else:
        assert found == expected


SUBMISSIONS_8K = {
    "filings": {
        "recent": {
            "form": ["8-K", "8-K", "6-K"],
            "items": ["2.02,9.01", "5.02,9.01", ""],
            "accessionNumber": ["0001326801-26-000001", "0001326801-26-000002", "0001326801-26-000003"],
            "acceptanceDateTime": ["2026-07-29T20:03:23.000Z", "2026-08-02T20:00:00.000Z", "2026-08-03T20:00:00.000Z"],
        },
        "files": [],
    }
}
RELEASE_8K = b"<html><body><p>EX-99.1 Exhibit 99.1</p><h1>Meta Reports Second Quarter 2026 Results</h1>" + b"<p>x</p>" * 20000 + b"</body></html>"


class Fake8kHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        return SUBMISSIONS_8K

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith("/000132680126000001/index.json"):
            return listing(("meta-20260729.htm", 40_000), ("meta-06302026xexhibit991.htm", 300_000))
        if url.endswith("/000132680126000001/meta-06302026xexhibit991.htm"):
            return RELEASE_8K
        raise AssertionError(f"unexpected request: {url}")  # the 8-K without item 2.02 and the 6-K must not be looked at


def test_an_8k_counts_only_with_item_2_02_and_exhibit_names_like_exhibit991_are_found(tmp_path):
    http = Fake8kHttp()
    found = SecProvider(http, tmp_path).results_announcements("META", 1326801, datetime(2026, 1, 1), "8-K")
    assert [(f.title, f.published) for f in found] == [("Meta Reports Second Quarter 2026 Results", pd.Timestamp("2026-07-29 16:03:23"))]
    assert len(http.requests) == 2  # the file list of the one earnings 8-K and its exhibit: nothing else was opened


STATEMENTS_HTML = b"<html><body><p>EX-99.3 4 financial_statementsxusx.htm EX-99.3 Exhibit 99.3 ASML - Summary US GAAP Consolidated Statements of Operations</p>" + b"<p>x</p>" * 20000 + b"</body></html>"
IFRS_HTML = b"<html><body><p>EX-99.5 6 d854985dex995.htm EX-99.5 Exhibit 99.5 ASML - Summary IFRS Consolidated Statement of Profit or Loss</p>" + b"<p>x</p>" * 20000 + b"</body></html>"


def test_statements_title_tells_the_us_gaap_summary_from_the_ifrs_one_and_a_press_release():
    from valuation_monitor.providers.sec import statements_title

    assert statements_title(STATEMENTS_HTML) == "Summary US GAAP financial statements"
    assert statements_title(IFRS_HTML) is None
    assert statements_title(RELEASE_8K) is None
    assert statements_title(b"<html><body><p>ASML reports 6.2 billion sales. Reconciliation to US GAAP Consolidated Statements of Operations is in exhibit 99.3</p></body></html>" + b"x" * 40000) is None


class FakeStatementsHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        return {"filings": {"recent": {"form": ["6-K"], "accessionNumber": ["0000937966-26-000001"], "acceptanceDateTime": ["2026-10-14T10:01:18.000Z"]}, "files": []}}

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith("/index.json"):
            return listing(("form6-kquarterlyfilings.htm", 11_000), ("pressreleasefinancialresul.htm", 41_000), ("financial_statementsxusx.htm", 28_000), ("presentationinvestorrela.htm", 20_600))
        if url.endswith("financial_statementsxusx.htm"):
            return STATEMENTS_HTML
        raise AssertionError(f"unexpected request: {url}")  # the press release, the presentation and the form itself are not downloaded


def test_the_statements_mode_keeps_the_us_gaap_summary_not_the_press_release(tmp_path):
    http = FakeStatementsHttp()
    found = SecProvider(http, tmp_path).results_announcements("ASML", 937966, datetime(2026, 1, 1), "6-K", "us_gaap_statements")
    assert [(f.title, f.url.rsplit("/", 1)[-1]) for f in found] == [("Summary US GAAP financial statements", "financial_statementsxusx.htm")]
    assert found[0].content == STATEMENTS_HTML and found[0].published == pd.Timestamp("2026-10-14 06:01:18")
