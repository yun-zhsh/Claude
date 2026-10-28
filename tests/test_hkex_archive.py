import json
from datetime import datetime

from valuation_monitor.providers.hkex import HkexProvider

URL = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0812/2026081200296.pdf"
FOUND = [
    {
        "title": "ANNOUNCEMENT OF THE RESULTS FOR THE THREE AND SIX MONTHS ENDED 30 JUNE 2026",
        "published": "2026-08-12T16:31:00",
        "url": URL,
    }
]


class FakeHttp:
    def __init__(self):
        self.downloads = []

    def get_bytes(self, url, **kwargs):
        self.downloads.append(url)
        return b"%PDF fake"


class FakeProvider(HkexProvider):
    online = True

    def _search(self, symbol, since):
        if not self.online:
            raise ConnectionError("offline")
        return FOUND


def test_archive_saves_once_and_works_offline(tmp_path):
    http = FakeHttp()
    provider = FakeProvider(http, archive_dir=tmp_path)

    first = provider.results_announcements("0700.HK", datetime(2026, 1, 1))
    assert http.downloads == [URL]
    assert (tmp_path / "0700.HK" / "2026-08-12_2026081200296.pdf").read_bytes() == b"%PDF fake"
    index = json.loads((tmp_path / "0700.HK" / "index.json").read_text(encoding="utf-8"))
    assert [e["file"] for e in index] == ["2026-08-12_2026081200296.pdf"]

    provider.results_announcements("0700.HK", datetime(2026, 1, 1))
    assert len(http.downloads) == 1  # 已保存的公告不再下载

    provider.online = False
    offline = provider.results_announcements("0700.HK", datetime(2026, 1, 1))
    assert [(f.title, f.published, f.pdf) for f in offline] == [(f.title, f.published, f.pdf) for f in first]
    assert provider.results_announcements("0700.HK", datetime(2026, 9, 1)) == []
