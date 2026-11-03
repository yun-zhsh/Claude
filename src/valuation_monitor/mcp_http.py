"""Serving the MCP server over HTTP (Google Cloud Run), with the limits a public, unauthenticated endpoint needs.

The data is public (original filings and prices, the same as the GitHub Pages site) and the service can only read it, so the risk of an
open endpoint is abuse and cost, not leaks. The guards:

  - a per-client and a global request rate limit (HTTP 429 when exceeded; the numbers are per server instance),
  - a small request body limit (set on the MCP library's own check),
  - stateless requests, no sessions kept in memory, and POST only (nothing can hold a stream open),
  - one log line per request with a short hash of the client address (never the address itself),
  - Cloud Run settings in scripts/deploy-cloud.sh: few instances, a service account that can only read the bucket.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable

logger = logging.getLogger("valuation_monitor.mcp")

WINDOW_SECONDS = 60.0
MAX_TRACKED_CLIENTS = 10_000


def client_key(scope) -> str:
    """The caller's address. Behind Cloud Run the last X-Forwarded-For entry is the one its front end added (earlier ones can be forged)."""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            parts = [p.strip() for p in value.decode("latin-1").split(",") if p.strip()]
            if parts:
                return parts[-1]
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimit:
    """ASGI middleware: at most `per_client` requests per minute from one address and `total` from all, counted over a sliding minute."""

    def __init__(self, app, per_client: int = 60, total: int = 600, clock: Callable[[], float] = time.monotonic):
        self.app = app
        self.per_client = per_client
        self.total = total
        self.clock = clock
        self._clients: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _allow(self, key: str) -> float | None:
        """None if the request may go ahead, else the number of seconds to wait."""
        now = self.clock()
        cutoff = now - WINDOW_SECONDS
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        hits = self._clients.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(self._all) >= self.total:
            return max(1.0, self._all[0] + WINDOW_SECONDS - now)
        if len(hits) >= self.per_client:
            return max(1.0, hits[0] + WINDOW_SECONDS - now)
        hits.append(now)
        self._all.append(now)
        if len(self._clients) > MAX_TRACKED_CLIENTS:  # forget addresses that have been quiet for a minute
            self._clients = {k: v for k, v in self._clients.items() if v and v[-1] > cutoff}
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key = client_key(scope)
        who = hashlib.sha256(key.encode()).hexdigest()[:8]
        status = {"code": 0}

        async def logging_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        wait = self._allow(key)
        if wait is not None:
            body = json.dumps({"error": f"Too many requests. Try again in {int(wait) + 1} seconds."}).encode()
            await logging_send({"type": "http.response.start", "status": 429, "headers": [(b"content-type", b"application/json"), (b"retry-after", str(int(wait) + 1).encode())]})
            await logging_send({"type": "http.response.body", "body": body})
        else:
            await self.app(scope, receive, logging_send)
        logger.info("mcp %s %s client=%s status=%s", scope["method"], scope["path"], who, status["code"])


class PostOnly:
    """ASGI middleware: only POST is answered. The server keeps no sessions, so it has nothing to stream to a GET, and an open
    stream would hold a connection (and a slot of the instance) for as long as the caller likes. MCP clients accept the 405."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] != "POST":
            await send({"type": "http.response.start", "status": 405, "headers": [(b"allow", b"POST"), (b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error": "Only POST is supported"}'})
            return
        await self.app(scope, receive, send)


def build_app(server, per_client: int = 60, total: int = 600):
    """The ASGI application for `server` (a FastMCP built with http=True): POST only, rate limited."""
    return RateLimit(PostOnly(server.streamable_http_app()), per_client, total)


def serve(server, host: str, port: int, per_client: int, total: int) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(build_app(server, per_client, total), host=host, port=port, log_level="warning", access_log=False)
