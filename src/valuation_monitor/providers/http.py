"""带磁盘缓存和重试的 HTTP 请求。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import requests

# Yahoo 会对完整的浏览器 UA 返回 429，简短 UA 反而可以正常访问
USER_AGENT = "Mozilla/5.0"


class HttpClient:
    def __init__(
        self,
        cache_dir: str | Path = ".cache",
        ttl_seconds: int = 12 * 3600,
        refresh: bool = False,
        user_agent: str = USER_AGENT,
        min_interval: float = 0.0,
    ):
        """min_interval：两次真正的网络请求之间至少间隔多少秒（命中缓存的不算），用于遵守数据源的限速规定。"""
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.refresh = refresh
        self.min_interval = min_interval
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def get_bytes(
        self, url: str, params: dict | None = None, immutable: bool = False, cache: bool = True, retries: int = 3
    ) -> bytes:
        """immutable=True 表示内容不会变，缓存永不过期，--refresh 也不重新下载；cache=False 不读写缓存（调用方自己保存）。"""
        key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()[:24]
        path = self.cache_dir / key
        if cache and path.exists() and (
            immutable or (not self.refresh and time.time() - path.stat().st_mtime < self.ttl_seconds)
        ):
            return path.read_bytes()

        for attempt in range(retries):
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=60)
                resp.raise_for_status()
                data = resp.content
                break
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(2**attempt)

        if cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return data

    def post_json(self, url: str, data: dict, retries: int = 5) -> dict:
        """POST 表单并返回 JSON（如巨潮资讯的公告查询）。结果按请求内容缓存（同 get_bytes 的缓存时间）。"""
        key = hashlib.sha256(json.dumps([url, data], sort_keys=True).encode()).hexdigest()[:24]
        path = self.cache_dir / key
        if path.exists() and not self.refresh and time.time() - path.stat().st_mtime < self.ttl_seconds:
            return json.loads(path.read_bytes())
        result = self._send("post", url, retries, data=data).json()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False))
        return result

    def get_response(self, url: str, retries: int = 5) -> requests.Response:
        """下载文件并返回完整响应（需要读响应头时用，如文件的上传时间）。不缓存，由调用方自己保存。"""
        return self._send("get", url, retries)

    def _send(self, method: str, url: str, retries: int, **kwargs) -> requests.Response:
        for attempt in range(retries):
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self.session.request(method, url, timeout=90, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(2 * (attempt + 1))
        raise RuntimeError("unreachable")

    def get_json(self, url: str, params: dict | None = None) -> dict:
        return json.loads(self.get_bytes(url, params))
