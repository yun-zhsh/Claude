"""Is the saved data fresh? A price or FX source that silently stops delivering shows up here as data that stays on the same day.

Prices and FX rates older than their limit are "stale": the update job (and the scheduled workflow) then fails, after saving
and publishing what it has, so that someone looks. The limits are calendar days (config.yaml, `freshness`), by market, long enough
for weekends and holidays; the newest day of the Yahoo-only markets can also arrive about a day late (see the README).

The newest filing of a company being old is only a warning: nothing here can fix a company that has not published.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from valuation_monitor.config import Config
from valuation_monitor.db import Database


@dataclass(frozen=True)
class Finding:
    kind: str  # "price", "fx" or "filing"
    subject: str
    last: pd.Timestamp | None  # the newest saved day, None if nothing is saved
    age_days: int | None
    limit: int
    blocking: bool  # a stale finding fails the job only if it is blocking

    @property
    def stale(self) -> bool:
        return self.last is None or self.age_days > self.limit

    @property
    def status(self) -> str:
        return "OK   " if not self.stale else ("STALE" if self.blocking else "WARN ")

    def line(self) -> str:
        if self.last is None:
            return f"{self.status} {self.subject}: nothing is saved"
        return f"{self.status} {self.subject}: newest {self.last:%Y-%m-%d}, {self.age_days} days old (limit {self.limit})"


def today() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()


def _finding(kind: str, subject: str, newest: str | None, limit: int, blocking: bool, now: pd.Timestamp) -> Finding:
    last = pd.Timestamp(newest[:10]) if newest else None
    return Finding(kind, subject, last, None if last is None else int((now - last).days), limit, blocking)


def check(db: Database, config: Config, now: pd.Timestamp | None = None) -> list[Finding]:
    """Every price series, FX pair and company's filings with how old its newest saved day is."""
    now = now if now is not None else today()
    limits = config.freshness
    findings = []
    with db.connect() as conn:
        for stock in config.stocks:
            newest = conn.execute("SELECT MAX(date) FROM prices_final WHERE symbol = ?", (stock.symbol,)).fetchone()[0]
            findings.append(_finding("price", f"{stock.symbol} prices", newest, limits.max_price_age_days[stock.market], True, now))
        for (pair,) in conn.execute("SELECT DISTINCT pair FROM fx_rates ORDER BY pair").fetchall():
            newest = conn.execute("SELECT MAX(date) FROM fx_rates WHERE pair = ?", (pair,)).fetchone()[0]
            findings.append(_finding("fx", f"{pair} FX rate", newest, limits.max_fx_age_days, True, now))
    for stock in config.stocks:
        announcements = db.read_frame("announcements", stock.symbol, text=True)
        newest = None if announcements is None else max(announcements["published"])
        findings.append(_finding("filing", f"{stock.symbol} filings", newest, limits.max_filing_age_days, False, now))
    return findings


def report(findings: list[Finding]) -> bool:
    """Print the findings (the problems to stderr) and return True if any blocking one is stale."""
    import sys

    for f in findings:
        print(f.line(), file=sys.stderr if f.stale else sys.stdout)
    return any(f.stale and f.blocking for f in findings)
