"""The MCP server on the real saved data, under the conditions of the Cloud Run service: no secret, nothing writable.

The synthetic data of test_mcp_server.py only has one stock and a writable folder, which is how a write to a read-only bucket
(JD: index.json) went unnoticed. These tests use the committed data of every configured stock and refuse every write under it.
"""

import errno
import pathlib
from pathlib import Path

import pytest

from valuation_monitor.mcp_server import open_service

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def read_only_data(monkeypatch):
    """Every attempt to write, create or delete anything under data/ fails like a read-only mount (EROFS)."""
    data = (ROOT / "data").resolve()
    original_open, original_mkdir, original_unlink = pathlib.Path.open, pathlib.Path.mkdir, pathlib.Path.unlink

    def inside(path) -> bool:
        return data in Path(path).resolve().parents or Path(path).resolve() == data

    def refuse(path):
        raise OSError(errno.EROFS, "Read-only file system", str(path))

    def guarded_open(self, mode="r", *args, **kwargs):
        if any(c in mode for c in "wax+") and inside(self):
            refuse(self)
        return original_open(self, mode, *args, **kwargs)

    def guarded_mkdir(self, *args, **kwargs):
        if inside(self) and not self.exists():
            refuse(self)
        return original_mkdir(self, *args, **kwargs)

    def guarded_unlink(self, *args, **kwargs):
        if inside(self):
            refuse(self)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", guarded_open)
    monkeypatch.setattr(pathlib.Path, "mkdir", guarded_mkdir)
    monkeypatch.setattr(pathlib.Path, "unlink", guarded_unlink)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)  # the public service has no secret


@pytest.fixture
def real_service(read_only_data):
    return open_service(str(ROOT / "config.yaml"), str(ROOT / "data"))


def test_every_configured_stock_loads_without_a_secret_or_write_access(real_service):
    result = real_service.list_stocks()
    assert [s["symbol"] for s in result["stocks"]] == [s.symbol for s in real_service.config.stocks]
    failed = {s["symbol"]: s["error"] for s in result["stocks"] if "error" in s}
    assert not failed, failed


def test_every_metric_of_every_stock_can_be_asked_for(real_service):
    for stock in real_service.config.stocks:
        for key in stock.metrics:
            result = real_service.valuation(stock.symbol, key, 5)
            assert result["sources"]["earnings_record"]["filing_url"], (stock.symbol, key)


def test_hikvision_before_its_annual_report_links_to_the_express_report(real_service):
    """The 2025 annual report came out on 2026-04-17; its figures replaced the express report of 2026-01-20 but kept that time."""
    result = real_service.valuation("002415.SZ", "pe", 5, "2026-03-02")
    record = result["sources"]["earnings_record"]
    assert record["period"] == "2025Q4" and record["preliminary"] is True
    assert record["first_release"]["kind"] == "express report" and record["filing_url"] == record["first_release"]["url"]
    assert record["figures_document"]["published"] == "2026-04-17 21:20" and record["figures_document"]["public_on_as_of"] is False
    assert any("may not have been public in their final form" in w for w in result["warnings"])
    after = real_service.valuation("002415.SZ", "pe", 5, "2026-04-20")
    assert after["sources"]["earnings_record"]["period"] == "2026Q1" and not [w for w in after["warnings"] if "final form" in w]


def test_samsung_within_the_statements_lag_is_marked_possibly_preliminary(real_service):
    """The English statements of 2026Q2 were uploaded after the call of 2026-07-30 (the day is not recorded): within 60 days they may not have been public."""
    during = real_service.valuation("005930.KS", "pe", 5, "2026-08-05")
    record = during["sources"]["earnings_record"]
    assert record["period"] == "2026Q2" and record["preliminary"] is True and record["filing_url"] is None
    assert any("no recorded publication day" in w for w in during["warnings"])
    later = real_service.valuation("005930.KS", "pe", 5, "2026-10-20")
    assert later["sources"]["earnings_record"]["preliminary"] is False and later["sources"]["earnings_record"]["filing_url"]


def test_providers_report_what_was_public_first(tmp_path):
    import json

    from valuation_monitor.providers.cninfo import CninfoProvider
    from valuation_monitor.providers.samsung import STATEMENTS_LAG_DAYS, SamsungIrProvider

    cn = tmp_path / "filings" / "000001.SZ"
    cn.mkdir(parents=True)
    (cn / "index.json").write_text(json.dumps([
        {"id": "1", "kind": "express", "year": 2025, "quarter": 4, "title": "2025 annual express report", "published": "2026-01-20T16:23:03", "url": "https://x/express.pdf", "file": "a.pdf"},
        {"id": "2", "kind": "report", "year": 2025, "quarter": 4, "title": "2025 annual report", "published": "2026-04-17T21:20:29", "url": "https://x/annual.pdf", "file": "b.pdf"},
        {"id": "3", "kind": "report", "year": 2025, "quarter": 3, "title": "2025 Q3 report", "published": "2025-10-17T17:52:07", "url": "https://x/q3.pdf", "file": "c.pdf"},
    ]))
    info = CninfoProvider(None, tmp_path / "filings").release_info("000001.SZ")
    assert list(info) == ["https://x/annual.pdf"]  # the quarterly report had no express report before it
    assert info["https://x/annual.pdf"].first_url == "https://x/express.pdf" and str(info["https://x/annual.pdf"].public_by) == "2026-04-17 21:20:29"

    kr = tmp_path / "filings" / "005930.KS"
    kr.mkdir(parents=True)
    (kr / "index.json").write_text(json.dumps([
        {"year": 2026, "quarter": 2, "published": "2026-07-30T10:00:00", "url": "https://x/q2.pdf", "files": {}},
        {"year": 2026, "quarter": 3, "published": "2026-10-29T10:00:00", "url": "https://x/deck.pdf", "files": {}, "provisional": True},
    ]))
    info = SamsungIrProvider(None, tmp_path / "filings").release_info("005930.KS")
    assert list(info) == ["https://x/q2.pdf"] and info["https://x/q2.pdf"].document_published is None
    assert (info["https://x/q2.pdf"].public_by - info["https://x/q2.pdf"].first_published).days == STATEMENTS_LAG_DAYS
