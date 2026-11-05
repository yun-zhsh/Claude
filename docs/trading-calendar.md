# Trading calendar: design

Status: **designed, not built.** This document is the complete specification of a new module that answers, for any date and any
exchange the project uses: is it a trading day, why not if it is not, what kind of trading day it is if it is, and what the
trading sessions are. It is written so that a developer (or a new coding session) who has read [CLAUDE.md](../CLAUDE.md) and this
file can build it without further discussion. Decisions already made are listed in section 2; the few left to the implementer are
in section 17.

Contents: 1 Purpose · 2 Decisions made · 3 Scope · 4 Concepts · 5 Data files · 6 Generator · 7 Read API · 8 Evidence and status ·
9 Reconciliation and the exceptions ledger · 10 Repair of price data · 11 Index data · 12 MCP tools · 13 Downloads · 14 Internal uses ·
15 Configuration changes · 16 Build plan (four pull requests) · 17 Left to the implementer · 18 Facts found while designing ·
19 Risks · 20 Not in scope

## 1. Purpose

A question like "was 2024-02-12 a trading day on the Hong Kong exchange, and why not?" cannot be answered today. The project has no
calendar. `src/valuation_monitor/market_calendar.py` holds closing times, a few half-day rules and a Lunar New Year table; which days
are trading days is inferred from the price data that exists (a missing price means closed). That gives no reason, nothing about the
future, and no way to notice that the price data itself has a gap.

The module has three uses, in this order of delivery:

1. **Ask through the MCP server** (and so from any AI assistant): the day, its kind, its reason, its sessions, with sources.
2. **Download** the calendar as CSV (Excel) and ICS (to subscribe in a calendar app).
3. **Use inside the program** when it builds the price database: check that the saved prices agree with the calendar, count staleness
   in trading days instead of calendar days, and later replace the hard-coded rules of `market_calendar.py`.

The design follows the project's principles: data from independent sources, **cross-checked** against each other, with a loud failure
instead of a guess; every answer carries its **source and its confidence**; the running program only **reads** (read-only, offline).

## 2. Decisions made

| # | Decision |
| --- | --- |
| 1 | **Approach A with a layer of B.** The calendar is generated outside the program from sources plus curated corrections, committed as data files and only read at run time. At run time the program checks the saved prices against it and upgrades "predicted" days to "confirmed", or reports a conflict. It never edits the files. |
| 2 | **Exchanges (first step):** HKEX, NASDAQ, KRX, SZSE, NSE (India). Not yet: NYSE, SSE, BSE. They are **not assumed equal** to a sibling: each exchange has its own calendar, and a stock on an exchange that has none is a configuration error. |
| 3 | **Range:** from **2000-01-01** to **two years ahead** of the date of generation. |
| 4 | **Sessions:** the regular trading sessions only (including the lunch break). Opening and closing auctions are notes. No pre-market or after-hours. |
| 5 | **Special closures** (typhoon, national mourning, temporary holidays) are found by evidence (price data) and the **reason is added from an announcement and cited**. When no source can be found the reason is recorded as "reason not verified". Nothing is invented. |
| 6 | **Indices as evidence.** One index per exchange is stored with the prices and used as evidence of which days traded. It is also useful later for relative performance. |
| 7 | **Three-way reconciliation** (calendar, index, stocks) on every price update and when a stock is added; a disagreement is shown in the program and investigated outside it. Either the index or a stock's data may be the one that is wrong. |
| 8 | **Recent disagreements alert, old ones are reported.** Plus a committed **known-exceptions ledger**. |
| 9 | **Repair of price data** is a separate manual command that records its source; the normal update never overwrites silently. |
| 10 | **Order of work:** data and MCP first, then downloads, then internal use. One pull request per step. Version **0.5.0** when done. |
| 11 | **Unannounced future:** for dates beyond what an exchange has published, the answer says "not yet announced; predicted by rule" with lower confidence. A-shares and India matter most here. |

## 3. Scope

**Exchanges** (code, name, time zone, MIC) and the stocks they serve today:

| Code | Exchange | Time zone | MIC | Stocks in `config.yaml` | Evidence index (Yahoo) |
| --- | --- | --- | --- | --- | --- |
| `HKEX` | Hong Kong Exchanges and Clearing (main board and GEM) | Asia/Hong_Kong | XHKG | 0700.HK, 2382.HK | `^HSI` |
| `NASDAQ` | Nasdaq Stock Market | America/New_York | XNAS | META, CTSH, ASML, JD | `^IXIC` |
| `KRX` | Korea Exchange (KOSPI and KOSDAQ share one calendar) | Asia/Seoul | XKRX | 005930.KS | `^KS11` |
| `SZSE` | Shenzhen Stock Exchange | Asia/Shanghai | XSHE | 002415.SZ | `399001.SZ` |
| `NSE` | National Stock Exchange of India | Asia/Kolkata | XNSE | none yet | `^NSEI` (from 2007-09) |

India has no stock, so its days are confirmed by the index only. NSE and BSE share their holidays in practice; BSE can be added later
as its own calendar.

**Within a calendar:** every calendar date in range, for every exchange (a full table, not only the exceptions). All dates are the
exchange's **local** dates; sessions are local times.

**Out of scope** (section 20): NYSE, SSE, BSE and other exchanges, extended hours, FX market hours, intraday data.

## 4. Concepts

**Exchange vs market.** `market` (HK, US, KR, CN) already exists in `config.yaml` and selects how filings are read. `exchange` is new
and selects the calendar. They are separate fields. `market: US` covers several exchanges, so a US stock must name its exchange.

**Day record** (what a lookup returns; section 7 has the exact fields):

* `status`: `open` or `closed` for the **regular** session.
* `kind`:
  * closed: `weekend`, `holiday` (regular holiday, with its English name), `special_closure` (unplanned or declared late: typhoon,
    mourning, temporary holiday), `workday_closed` (China: a Saturday or Sunday that the government makes a working day to offset a
    holiday; the exchange stays closed, and the answer must say so),
  * open: `regular`, `half_day` (shortened session), `special_hours` (different times on a day that is open), `special_session`
    (a session on a day on which the regular session is closed: India's Diwali evening session, a Saturday session).
* `sessions`: the sessions of that day as a list of `HH:MM-HH:MM`, each with a label (`regular`, `morning`, `afternoon`, `special`).
* `regular_sessions`: what a normal day of that exchange looked like on that date, so an answer can say "half a day: 09:30-12:00
  instead of 09:30-12:00 and 13:00-16:00". Sessions changed over the years (section 18), so "normal" depends on the date.

**Basis** of a record in the committed file (where the knowledge comes from):

* `announced`: taken from an exchange or government announcement or an authoritative published list;
* `rule`: computed from rules, because the exchange has not published that period yet (the future);
* `observed`: known only from price data, **reason not verified**.

**Verification** (also in the file, from the generation step): `verified` is `yes` when the evidence (section 8) agrees with the
record, `no` for dates after the last evidence, and `conflict` is never committed (the generator refuses). `verified_by` names the
evidence (for example `index ^HSI`).

**Status** (not stored; computed when a question is asked, section 8): `confirmed`, `predicted` or `conflict`.

**Published horizon**: per exchange, the last date through which the exchange has published its calendar (`announced_through` in the
metadata). Dates after it can only be `rule`. A-shares and India publish about a year ahead at best (China's State Council announces
the holidays and the make-up working days for a year in the preceding November or December); Hong Kong and Korea publish about a
year; the US publishes years ahead.

## 5. Data files

Everything the program reads at run time is **package data**, shipped in the container image with the code and versioned with it.
Outputs of the generator live in `src/valuation_monitor/calendars/`; the human-edited inputs of the generator live in `tools/calendars/`
(not in the image).

```
src/valuation_monitor/calendars/
  exchanges.json            metadata of every exchange (below)
  HKEX.csv NASDAQ.csv KRX.csv SZSE.csv NSE.csv      one row per calendar date (below)
  known_exceptions.csv      the ledger of section 9
tools/calendars/
  build.py                  the generator (section 6)
  inputs/<EXCHANGE>/
    holidays.csv            names and corrections of regular holidays
    special_closures.csv    one row per unplanned closure: date, name, reason, source, source_url
    special_sessions.csv    sessions on closed days: date, sessions, name, source, source_url
    sessions.csv            the regular and half-day sessions with the date they take effect: effective_from, effective_to,
                            label, sessions, note, source
  requirements.txt          libraries the generator uses (they are not run-time dependencies)
```

**`<EXCHANGE>.csv`** columns (English only, ISO dates, UTF-8, `\n` line ends, sorted by date, no blank lines):

| Column | Meaning |
| --- | --- |
| `date` | local date, `YYYY-MM-DD` |
| `status` | `open` or `closed` |
| `kind` | one of the kinds in section 4 |
| `name` | English name: the holiday ("Lunar New Year"), the event ("Typhoon signal No. 8"), or empty for a plain weekend or regular day |
| `closed_reason` | for `special_session` only: why the regular session is closed (a holiday name or `weekend`) |
| `sessions` | `09:30-12:00;13:00-16:00` (empty when closed) |
| `regular_sessions` | the normal sessions of that exchange on that date |
| `note` | free text, for example "closing auction 16:00-16:10" |
| `basis` | `announced`, `rule` or `observed` |
| `reason_verified` | `yes`, `no`; `no` only for `special_closure` and `observed` rows whose reason has no source |
| `verified` | `yes` or `no` (see section 4) |
| `verified_by` | for example `index ^HSI` |
| `source` | short source name, for example `HKEX holiday schedule 2026` |
| `source_url` | link, may be empty for rules |

**`exchanges.json`**: per exchange: `code`, `name`, `mic`, `timezone`, `coverage_from`, `coverage_to`, `announced_through`,
`generated_on` (date), `generator_version`, `evidence_index` (symbol and name), `libraries` (names and versions used), `sources`
(names and links). The read API (section 7) and the MCP answers quote `generated_on` and `announced_through`.

The packaging has a trap: `pyproject.toml` lists package data by name (`valuation_monitor.web`: templates and static files). Add
`"valuation_monitor.calendars" = ["*.csv", "*.json"]`, and check in the container smoke test of `.github/workflows/ci.yml` that the files
are in the image. The `Dockerfile` already copies `src/`; `data/` is **not** in the image, which is why the calendar is not under `data/`.

## 6. Generator

`tools/calendars/build.py` is run by hand, a few times a year, never by the program, never in CI (it needs the network). CI only checks the
committed output (section 16).

Steps for each exchange:

1. **Base calendar** from an open-source library used as a starting point (to be evaluated, section 17: `exchange_calendars`,
   Apache-2.0, has calendars for XHKG, XNAS, XKRX, XSHE and Indian exchanges; `holidays`, MIT, has names of public and financial holidays).
   The libraries are not run-time dependencies and are listed in `tools/calendars/requirements.txt`; their versions are recorded in
   `exchanges.json`.
2. **Apply the curated inputs** (`tools/calendars/inputs/<EXCHANGE>/`): correct names to English; add the unplanned closures and
   special sessions; set the sessions for each period from `sessions.csv`. Every curated row has a source.
3. **Mark the basis:** `announced` up to the exchange's published horizon, `rule` after it, `observed` for days that the evidence shows
   closed (or open) against the sources and whose reason is not documented yet.
4. **Verify** against evidence (section 8): download the daily history of the evidence index of the exchange (Yahoo, from 2000-01-01
   or its first bar) and compare every date that has evidence:
   * a date with an index value must be `open` (any kind but `special_session` on a closed day is fine);
   * a **weekday** without an index value must be `closed`, and if it is `closed` it must have a reason (`holiday`, `special_closure`,
     `workday_closed`) or be recorded as `observed` with `reason_verified = no`;
   * a weekend date with an index value must be `special_session`.
   An index value is a non-null close. Old Yahoo history has a row for every weekday with a **null** close on non-trading days: drop
   those rows. A close identical to the previous day's is suspect and is not evidence of trading.
5. **Write** the CSV files and `exchanges.json`, sorted and byte-stable (the same inputs give the same files, so a diff shows only real changes).
6. **Report**: counts per kind and basis, the dates verified, the dates beyond the evidence, every conflict. The generator **exits
   non-zero while any conflict is unexplained**. A conflict is resolved by correcting an input with a source, or (for a data error in the
   evidence itself) by an entry in `known_exceptions.csv` (section 9).

Special closures are the manual part. Seed lists to research and verify against announcements before relying on them are in
section 18. Every row in `special_closures.csv` needs `source` and `source_url`; when none is found the row is written with
`reason_verified = no`.

## 7. Read API

A new module `src/valuation_monitor/trading_calendar.py`. Only reads the package data (no network, no writes, no DB needed for the base
answer). It is imported by the MCP server, the export commands and later the update code.

```python
@dataclass(frozen=True)
class Session:
    start: time; end: time; label: str            # "regular", "morning", "afternoon", "special"

@dataclass(frozen=True)
class DayInfo:
    exchange: str; date: date; weekday: str; timezone: str
    status: str                                    # "open" | "closed"
    kind: str                                      # section 4
    name: str; closed_reason: str
    sessions: list[Session]; regular_sessions: list[Session]; note: str
    basis: str; reason_verified: bool
    verified: bool; verified_by: str
    announced_through: date; generated_on: date; coverage: tuple[date, date]
    warnings: list[str]                            # e.g. "after the published horizon: predicted by rule"

class TradingCalendar:
    def day(self, exchange: str, day: date) -> DayInfo
    def is_open(self, exchange: str, day: date) -> bool
    def days(self, exchange: str, start: date, end: date) -> list[DayInfo]
    def trading_days(self, exchange: str, start: date, end: date) -> list[date]
    def next_open(self, exchange: str, day: date) -> date
    def previous_open(self, exchange: str, day: date) -> date
    def exchanges(self) -> list[ExchangeInfo]
```

Rules:

* A date outside `coverage` **raises** `CalendarRangeError` with a message that names the coverage and says to regenerate. It never guesses.
* An unknown exchange raises with the list of known ones.
* Dates beyond `announced_through` are answered, with `basis = rule` and a warning, never silently.
* The file is read once per exchange and cached; lookups are dictionary lookups.
* A helper maps a stock to its exchange (section 15).

The base `DayInfo` is **expected** knowledge. Status (confirmed, predicted, conflict) is added by `trading_status.py` (section 8), which
also needs the price database; the base module does not.

## 8. Evidence and status

Two independent kinds of information describe each date of an exchange:

* **Expected**: the calendar file (sources and rules).
* **Observed**: the price data: the evidence index of the exchange (primary) and the saved prices of the stocks on that exchange
  (secondary). From the index: `traded` when a non-null close exists and is not a duplicate of the previous close; `no_trade` when it
  does not exist **and a later date has a value** (the feed was working, so the absence means something); `pending` otherwise (the newest
  days, because the Yahoo-only markets can lag a day, and any day after which nothing arrived yet). A stock is observed the same way,
  from `prices_final` (a day exists when the consolidated price exists; the HK stocks also have Tencent as a second source).

Status of a date:

| Expected | Observed | Status |
| --- | --- | --- |
| open | traded | `confirmed` |
| closed | no_trade | `confirmed` |
| any | pending | `predicted` |
| open | no_trade | `conflict` |
| closed | traded | `conflict` (unless `kind = special_session`: then it is `confirmed`) |

`special_session` is the one case where a closed day has data. A weekend special session, however, is **not visible in Yahoo data**
(found while designing, section 18), so it is recorded from the exchange's circular only and cannot be confirmed by data.

A `conflict` carries its evidence: which sources disagree with the calendar, how many stocks, whether later days exist, and a
diagnosis (section 9).

`trading_status.py` takes the calendar and read access to the DB and returns `DayStatus(day_info, observed, status, evidence, diagnosis)`.
It is the same code the generator uses for its verification and the update job uses for reconciliation: **one implementation**, so the
rules cannot drift apart. It never writes.

## 9. Reconciliation and the exceptions ledger

**When it runs:** after every price update (for the new days), when a stock is added (over its whole history), and after the calendar
file or index data is replaced. It is part of `valuation-monitor-update` and of `valuation-monitor-check`.

**What it compares:** for each exchange and each date, three sources: calendar, index, each stock. Two of three agreeing decides who is
probably wrong. Do not trust any one of them. The hints written into the finding:

| Calendar | Index | Stock | Most likely | Look first |
| --- | --- | --- | --- | --- |
| open | traded | missing | stock data gap, or the stock was halted | the company's announcements; if not a halt, patch from another source (section 10) |
| open | missing | missing | the exchange really was closed (typhoon, mourning), or the data source had a gap for everything | the exchange's notices; add the closure to `special_closures.csv` with a source |
| open | missing | traded | index data gap | refetch the index; compare with another index source |
| closed | traded | traded | the calendar is wrong, or there was a special session | the exchange's notices; correct the calendar |
| closed | traded | missing | index has an extra value | compare with another index source |
| closed | missing | traded | the stock has an extra or duplicated value | check the stock's data source |

**Common-source risk.** The index and most stocks come from Yahoo. If Yahoo fails for one day for everything, index and stocks agree
with each other and disagree with the calendar: it looks like a closure. Such a finding is "suspected closure" until a notice is found and
recorded with a source. For HK the Tencent quote API is a second, independent source of stock prices.

**Severity and where it shows:**

* **High confidence** (the index and at least two stocks on the exchange agree with each other against the calendar, or, for an exchange
  with fewer stocks, the index and the stock agree, and later days exist): a finding in the **last 10 trading days** makes the update fail
  (the data is saved and the site published first, as the freshness check does; the failed run sends the email).
* **Low confidence** (one source only, or `pending`): a warning in the log and in the MCP answer, never a failure.
* Findings **older than 10 trading days** do not fail the run (a refetch cannot change them); they go in the data-quality report
  (`valuation-monitor-check`) until they are resolved.

**The exceptions ledger**, `src/valuation_monitor/calendars/known_exceptions.csv`: a finding that has been investigated and explained is
recorded, and is then not reported again. Columns: `exchange`, `date`, `subject` (an index or stock symbol, or `calendar`), `observed`,
`expected`, `resolution` (`halt`, `source_gap_patched`, `calendar_updated`, `index_error`, `accepted`), `note`, `source`, `source_url`,
`added_on`. A finding not in the ledger is new. This keeps the check loud without making it permanently red, and A-share halts (frequent,
and a company event) have a place to be recorded. Entries only ever get added; the history is the audit trail.

## 10. Repair of price data

When a stock's data has a gap that is not a halt, the repair is a separate, **manual** command (to be named and built in the fourth pull
request or later, for example `valuation-monitor-repair`), never part of the normal update:

* It takes a symbol and a date range, fetches the missing days from an alternative source, and writes them to `price_bars` and
  `prices_final` with the source recorded in the existing `source` and `status` columns (for example status `patched`).
* An alternative source is itself cross-checked and is never trusted just because it is the alternative.
* It prints exactly what it changed and appends the ledger entry (`resolution = source_gap_patched`).
* Candidate sources to evaluate when this is built: the Tencent quote API (already used for HK; it is believed to serve A-shares too, to be
  tested), others for Korea and the US. No source is chosen in this design.

## 11. Index data

* Indices are stored in the existing `price_bars` table with the index symbol as `symbol` (`^HSI`, `^IXIC`, `^KS11`, `399001.SZ`,
  `^NSEI`) and `source = yahoo`; its meta document is `prices/<symbol>/yahoo` as for stocks. No new table.
* `config.yaml` gets a new block (the exchange codes of section 3 are the keys):

```yaml
indices:
  HKEX:   {symbol: "^HSI",      name: Hang Seng Index}
  NASDAQ: {symbol: "^IXIC",     name: Nasdaq Composite}
  KRX:    {symbol: "^KS11",     name: KOSPI Composite Index}
  SZSE:   {symbol: "399001.SZ", name: SZSE Component Index}
  NSE:    {symbol: "^NSEI",     name: Nifty 50}
```

* They are fetched and saved by the update job with the same incremental rules as stock prices (new days only, saved history never
  rewritten silently, a change in the overlap is reported).
* Indices have no volume. "Has a value" means a non-null close. Do not use the volume filter used for stocks.
* They are shown nowhere yet; their later use is relative performance against the market.
* The index of an exchange with no stock (NSE) is fetched too.

## 12. MCP tools

New tools in `mcp_server.py`, in the same four-rule spirit as the valuation tools ([mcp-server.md](mcp-server.md)): read-only, offline, sourced,
no arithmetic by the model.

* `list_exchanges()`: code, name, time zone, coverage, `announced_through`, `generated_on`, which exchange each configured stock is on.
* `get_trading_day(exchange_or_symbol, date)`: the `DayInfo` fields of section 7 plus `status` (confirmed, predicted, conflict) and its
  evidence from the DB when available. A stock symbol is accepted and mapped to its exchange. The answer lists warnings (beyond the
  horizon; reason not verified; weekend special sessions are not confirmable).
* `trading_days(exchange_or_symbol, start, end)`: the number of trading days, and the list of non-trading days in the range with reasons.
  Counting is done in Python.
* `next_trading_day(exchange_or_symbol, date)` and `previous_trading_day(...)`.

**Exception to the rule "a new tool needs an `as_of` date"** in [CLAUDE.md](../CLAUDE.md): the calendar tools answer a question about a
date in the world, not a valuation from data of that moment, so the date asked is the `date` parameter, and the answer says which data
it was generated from (`generated_on`, `announced_through`, `basis`). The implementation pull request edits that rule to say so.

The tests of the MCP server apply (read-only, no network, files untouched, production-condition tests on the committed data: every
configured exchange and every exchange in `exchanges.json`).

Answers are examples of what the model should be able to say from the result, for the guide in `mcp-server.md`:

* "2024-02-12 was a **closed** day on HKEX: Lunar New Year (second day), announced by HKEX; confirmed by the Hang Seng Index having no value."
* "2024-12-24 on HKEX was a **half day**: 09:30-12:00 instead of 09:30-12:00 and 13:00-16:00 (Christmas Eve)."
* "2027-03-15 on SZSE is **predicted open by rule**: China has not announced its 2027 holidays yet."

## 13. Downloads

* **CSV**, English column names, UTF-8: the committed file of the exchange, or a range of it, with the same columns as section 5 (the
  program adds `status` as computed in section 8 only where the DB is available, for example in the web export).
* **ICS** (iCalendar 2.0), one calendar per exchange: a closed day is an all-day event (`SUMMARY: HKEX closed: Lunar New Year`); a half
  day or a special session is a timed event with the exchange's `TZID`; `UID` is `<exchange>-<date>@valuation-monitor` (stable, so that a
  subscribed calendar updates in place); `X-WR-CALNAME` is `<exchange> trading calendar`; `DESCRIPTION` carries the kind, the source and
  `basis`. Regular open days are not events (a calendar of 250 identical events a year is noise).
* **Command line**: a new entry point, for example `valuation-monitor-calendar` with `export --exchange HKEX --from 2026-01-01 --to 2027-12-31
  --format csv|ics`, and `day --exchange HKEX --date 2024-02-12` printing the answer.
* **Web**: the static site export (`valuation-monitor-export`) gets a `calendar/` folder with `<EXCHANGE>.csv` and `<EXCHANGE>.ics`, and the
  site gets a page that links to them and shows a month grid per exchange. File names are English. The English-only tests cover them.

## 14. Internal uses

In this order, each behind its own tests:

1. **Staleness in trading days.** `freshness.py` today counts calendar days with limits by market (HK 5, US 5, KR 6, CN 10; the CN
   limit is long only for Golden Week). Replace the price limit with "the newest saved price is more than N trading days old", N in
   `config.yaml` (about 3), counted with the calendar of the stock's exchange. FX keeps calendar days (FX is not an exchange).
2. **Gap detection** is section 9 (reconciliation).
3. **"Is today's bar complete?"** `PriceStore` today never saves a bar dated today (it uses the local midnight of the exchange's
   time zone). With the sessions it can save a bar once the session has ended. Optional.
4. **Replace `market_calendar.py`'s hard-coded rules** (closing times, half-day rules, the Lunar New Year table) by lookups in the calendar,
   which also fixes the history (Korea's close was 15:00 before it became 15:30; Hong Kong's hours changed in 2011; see section 18). This
   changes how `available_date` is computed, so it must be shown not to change any result: compare, for every saved filing of every
   company, the effective trading day before and after (a golden snapshot, as the SQLite migration did). Expected: no change for filings since
   2014. If something moves, understand it before accepting it.

## 15. Configuration changes

`config.yaml`, per stock, gets `exchange`. Defaults by symbol suffix: `.HK` is `HKEX`, `.KS` and `.KQ` are `KRX`, `.SZ` is `SZSE`, `.SS` is `SSE`.
There is **no default for US stocks** (they have no suffix): write `exchange: NASDAQ` on META, CTSH, ASML and JD. A stock whose exchange
has no calendar (`NYSE`, `SSE`, `BSE`) fails at load time with a message saying which calendar to add.

`StockConfig` gets `exchange: str`. The existing `market` is unchanged. A test checks that every stock in `config.yaml` has a calendar.

`config.yaml` also gets the `indices` block of section 11, and (in the third pull request) the freshness limit in trading days.

Add a freshness-style warning (not an error) in `valuation-monitor-check`: the calendar's `announced_through` or `coverage_to` is less than
180 days ahead, so that the yearly regeneration is not forgotten. Do **not** make a CI test depend on today's date.

## 16. Build plan: four pull requests

Each is one pull request, merged before the next. After each, the full test suite passes on Python 3.10 and 3.12 (pandas 2 and 3: the last
round of bugs came from pandas 2).

**PR 1: calendar data, read API, MCP tools.**

* `tools/calendars/` generator, curated inputs, requirements; run it for the five exchanges; commit the outputs and `exchanges.json`.
* `trading_calendar.py` (section 7), `exchange` in `config.yaml` and `StockConfig` (section 15), package data, the container smoke test.
* MCP tools of section 12 (base answer; status and evidence from the DB can wait for PR 3, but the file's `verified` and `verified_by`
  already say which dates the generator confirmed), the `CLAUDE.md` rule exception, `docs/mcp-server.md`, README outline.
* Tests: the read API on a small fixture calendar (every kind, the range errors, the horizon warning); invariants on the committed files
  (every date of the coverage exactly once; weekends are `closed` unless `special_session`; kinds and bases valid; sessions parse and do
  not overlap; `closed_reason` only on `special_session`; names have no CJK; CSV is sorted and byte-stable when regenerated from the
  inputs); the **golden dates** of section 18 once verified; `tests/test_english_only.py` extended to scan `src/valuation_monitor/calendars/*`
  and `tools/calendars/inputs/**`; the MCP production-condition tests.
* Done when: the generator reports **no unexplained conflict** for the five exchanges from 2000-01-01 (or from the first evidence
  value), the golden dates pass, the MCP tools work against the running service (check with `curl`, as for the earlier deployments), and
  the deployment check in `scripts/deploy-cloud.sh mcp` also asks `list_exchanges` and fails if an exchange does not load.

**PR 2: downloads.** CSV, ICS, the command line entry point, `calendar/` in the static export, the web page. Tests: the ICS parses
(a small parser in the test, or a library used only in tests), `UID`s are stable across two exports, CSV round-trips, files are English.

**PR 3: indices, reconciliation, internal use.** Indices in `config.yaml` and the update job; `trading_status.py` (one implementation,
also called by the generator); reconciliation in `valuation-monitor-update` and `valuation-monitor-check` with the two tiers and the
ledger; the data-quality report; staleness in trading days; the MCP answer gets `status` and its evidence. Tests: every row of the
diagnosis table; `pending` for the newest days; a feed gap (later days exist) versus the newest-day lag; the ledger silences a known
finding; the three severities; the run fails on a high-confidence recent finding and not on an old one; production-condition tests with the
committed database and no network.

**PR 4: replace the hard-coded rules and add the repair command.** As in section 14 item 4 (with the golden snapshot) and section 10. Then
bump the version to **0.5.0** and tag the release.

## 17. Left to the implementer

* **Which libraries** for the base calendar, after trying them: check their accuracy against the golden dates and the index evidence from
  2000 on. If a library is wrong for a period, correct it in the curated inputs rather than depend on a fix upstream.
* **The exact set of special closures and special sessions** since 2000 for each exchange. Research, cite, verify against evidence.
* **Where a library has no calendar** for an exchange for the full range (for example before some year), write the base from the
  exchange's own published holiday lists and record the source.
* The names of the command line entry points and of the repair command.
* Whether the web month grid is worth building in PR 2, or whether links to the files are enough for a first version.

## 18. Facts found while designing

**Evidence indices (probe of 2026-11-04, Yahoo chart API).**

* For 2024, `^KS11`, `^HSI`, `^IXIC`, `399001.SZ`, `^NSEI` had **no row** on the holidays checked (Korea 2024-01-01, 2024-02-09, 2024-05-06,
  2024-12-31; HK 2024-02-12, 2024-03-29, 2024-05-15; Nasdaq 2024-03-29, 2024-06-19; Shenzhen 2024-02-12, 2024-05-01, 2024-10-01; Nifty
  2024-03-25), and a row on the Nasdaq half day 2024-07-03.
* **Weekend special sessions are not visible**: Nifty has no row on Saturday 2024-03-02 (an NSE special live session), but it does have a
  row on 2024-11-01 (the Diwali evening session, on a Friday).
* **Old history has nulls**: for 2000, `^HSI`, `^KS11`, `399001.SZ`, `000001.SS` and `^BSESN` have a row for **every weekday** (260), with a null
  close on non-trading days; drop null closes. `^IXIC` and `^NYA` have only trading days (252).
* **Earliest data**: `^HSI`, `^IXIC`, `^NYA` from 1990-01-02; `^KS11` from 1996-12-11; `399001.SZ` from 1997-08-22; `000001.SS` from
  1997-07-02; `^BSESN` from 1997-07-01; `^NSEI` has **no** data before 2007-09, so 2000 to 2007 on NSE cannot be confirmed by it.
* One duplicated close in 2024 in `^NSEI`: treat a close equal to the previous one as suspect.

**Sessions (believed, to verify against the exchanges' notices; then put in `sessions.csv` with sources):**

| Exchange | Believed regular sessions (local time) | Changes since 2000 |
| --- | --- | --- |
| HKEX | 09:30-12:00 and 13:00-16:00; closing auction 16:00-16:10 | morning 10:00-12:30 and afternoon 14:30-16:00 before March 2011; 09:30 opening from 2011-03-07; the afternoon opening later moved to 13:00 (2012); closing auction from 2016-07-25 |
| NASDAQ | 09:30-16:00 | none; early close 13:00 on the day after Thanksgiving, on Christmas Eve (weekday) and on 3 July in some years |
| KRX | 09:00-15:30 | close 15:00 before 2016-08-01; the first trading day of the year and the CSAT (university entrance exam) day open later (CSAT day: 10:00 to 16:30) |
| SZSE | 09:30-11:30 and 13:00-15:00 | closing call auction 14:57-15:00 from 2018 |
| NSE | 09:15-15:30 | 09:15 opening from the 2010 change (earlier 09:55); special live sessions on some Saturdays; the Diwali evening session (Muhurat) |

**Half days believed:** HKEX morning only on Christmas Eve, New Year's Eve and Lunar New Year's Eve (the existing code does this); NASDAQ as
above; none believed on KRX and SZSE.

**Special closures to research and cite** (believed; the list is a start, not complete):

* NASDAQ: 2001-09-11 to 2001-09-14; 2004-06-11 (Reagan); 2007-01-02 (Ford); 2012-10-29 and 2012-10-30 (Hurricane Sandy); 2018-12-05 (Bush);
  2025-01-09 (Carter).
* HKEX: days with tropical cyclone warning signal No. 8 or higher, and black rainstorm days, as the rules of the time said (they changed
  several times; since 2024-09-23 trading continues in severe weather, so none after that).
* KRX: temporary public holidays, for example the days of national elections declared by the government.
* SZSE: none known besides the announced holiday arrangements. The China make-up working days are not special closures: they are `workday_closed`.
* NSE: for example 2024-01-22 (a declared holiday); the special sessions of 2024-03-02 and 2024-11-01.

**Golden dates** to turn into tests after verifying each against its source (believed):

| Exchange | Date | Expected |
| --- | --- | --- |
| NASDAQ | 2024-03-29 | closed, holiday (Good Friday) |
| NASDAQ | 2024-07-03 | open, half day, 09:30-13:00 |
| NASDAQ | 2024-07-04 | closed, holiday (Independence Day) |
| NASDAQ | 2024-11-29 | open, half day, 09:30-13:00 |
| HKEX | 2024-02-09 | open, half day (Lunar New Year's Eve) |
| HKEX | 2024-02-12 | closed, holiday (Lunar New Year) |
| HKEX | 2024-10-11 | closed, holiday (Chung Yeung Festival) |
| KRX | 2024-02-12 | closed, holiday (Seollal substitute holiday) |
| KRX | 2024-05-06 | closed, holiday (Children's Day substitute holiday) |
| KRX | 2024-12-31 | closed (year-end closing) |
| SZSE | 2024-02-09 | closed, holiday (Spring Festival) |
| SZSE | 2024-02-18 | closed, `workday_closed` (a Sunday made a working day) |
| SZSE | 2024-10-12 | closed, `workday_closed` (a Saturday made a working day) |
| NSE | 2024-03-25 | closed, holiday (Holi) |
| NSE | 2024-11-01 | open, `special_session`, evening (Diwali), regular session closed |
| NSE | 2024-03-02 | open, `special_session`, Saturday |

## 19. Risks and how the design handles them

* **A wrong source.** Every record has a basis and a source; the generator verifies against independent evidence and refuses unexplained conflicts.
* **A wrong index, or a wrong stock series.** Three-way reconciliation; hints; the ledger; the repair command with a recorded source (sections 9, 10).
* **A common failure of Yahoo.** Reported as "suspected closure" until a notice is found; HK has a second source for stocks.
* **The calendar expires.** Out-of-range dates raise; a warning in `valuation-monitor-check` when less than 180 days remain; the yearly regeneration is a documented routine.
* **The future is not known.** Dates after the published horizon are `rule` with a warning; A-shares and India are the least predictable.
* **Weekend special sessions are invisible in the data.** Recorded from circulars only; the answer says they cannot be confirmed.
* **Large manual research for 2000 to 2013.** Special closures and session history are researched as far as sources allow; the rest is marked "reason not verified". The generator output does not depend on the research being complete.
* **Alert noise.** Two severity tiers, the 10-day window, and the ledger.
* **Changing the effective-day computation** (PR 4) could shift results: it is gated by a golden snapshot.

## 20. Not in scope

NYSE, SSE and BSE (add each when a stock needs it: a new calendar and its evidence index); other exchanges; pre-market and after-hours;
auction schedules beyond a note; intraday data; the FX market; relative performance (the stored indices make it possible later); a web
page that edits the calendar (it changes only through the generator and a pull request).
