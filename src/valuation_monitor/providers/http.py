"""带磁盘缓存和重试的 JSON 请求。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import requests

# Yahoo 会对完整的浏览器 UA 返回 429，简短 UA 反而可以正常访问
USER_AGENT = "Mozilla/5.0"


class HttpClient:
    def __init__(self, cache_dir: str | Path = ".cache", ttl_seconds: int = 12 * 3600, refresh: bool = False):
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.refresh = refresh
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def get_json(self, url: str, params: dict | None = None, retries: int = 3) -> dict:
        key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()[:24]
        path = self.cache_dir / f"{key}.json"
        if not self.refresh and path.exists() and time.time() - path.stat().st_mtime < self.ttl_seconds:
            return json.loads(path.read_text(encoding="utf-8"))

        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.RequestException, ValueError):
                if attempt == retries - 1:
                    raise
                time.sleep(2**attempt)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
