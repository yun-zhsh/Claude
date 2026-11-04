# Stock Valuation Monitor

A personal research tool that answers one question: **is a stock's valuation (PE, PS, PB) high or low compared with its own history?**
For five companies in four markets (Hong Kong, US, Korea, China A-shares) it reads the numbers from the companies' own
earnings filings, joins them with daily prices, and shows today's multiple as a percentile of the last 1 to 10 years.
Every number can be traced back to the filing it came from, and the page shows the exact calculation behind any day on a chart.

**Live demo** (read-only, refreshed every 4 hours): https://yun-zhsh.github.io/Watchlist-monitor/

![A stock page: percentile cards, a PE trend chart and the calculation behind one day](docs/stock-page.png)

*Hovering over a day on a trend chart shows the whole calculation: close price, FX rate, and the quarterly EPS it was built from.*

## Architecture

The same Python package runs in two places: on GitHub (a public static site) and on Google Cloud (a private dynamic page that only the
owner can sign in to, so the diagram is how everyone else sees it).

```mermaid
flowchart LR
  subgraph SRC["Public sources"]
    F["Company filings<br/>HKEXnews, SEC EDGAR,<br/>Samsung IR, CNINFO"]
    P["Prices and FX<br/>Yahoo Finance, Tencent"]
  end

  subgraph GH["GitHub (public)"]
    A["GitHub Actions<br/>every 4 hours"]
    R[("Repository<br/>data/valuation.db<br/>and original filings")]
    S["GitHub Pages<br/>static site"]
  end

  subgraph GCP["Google Cloud (private)"]
    SCH["Cloud Scheduler<br/>every 4 hours"]
    J["Cloud Run job<br/>update"]
    B[("Cloud Storage<br/>valuation.db")]
    W["Cloud Run service<br/>web page behind IAP<br/>(owner only)"]
  end

  F --> A
  P --> A
  A -- commit --> R
  A -- export --> S

  F --> J
  P --> J
  SCH --> J
  J <-- "download, upload with version check" --> B
  B -- "read a copy" --> W
  W -. "Update data button starts" .-> J
```

## Design decisions and trade-offs

**Original filings, not a data vendor.** The numbers come straight from the exchange and regulator filings (HKEXnews, SEC EDGAR,
Samsung's investor site, CNINFO), parsed by this project. That makes every figure verifiable, gives the exact publication time of
each one, and costs nothing. The price is one parser per filing layout (five so far) and maintenance when a layout changes.
This is a personal research project: prices come from free sources such as Yahoo Finance (cross-checked against Tencent for Hong Kong
stocks), whereas a production system would use licensed vendor data. All multiples are trailing, built from reported history. Forward
multiples need analyst estimates, which only professional data providers license; once such a series exists it should slot in as
one more per-share metric, because the percentile, charts and audit trail work on any per-share series.

**No look-ahead.** A reported figure is used only from the first trading day on which the market could have seen it: the publication time
(to the minute where the source gives it) is compared with each exchange's closing time, half days included, and the effective day is
taken from the days that actually traded. The cost is a calendar and a time rule per source, kept in `market_calendar.py` and the providers.

**Fail loudly rather than guess.** A filing that cannot be parsed raises an error instead of producing a best-effort number. The same
value is cross-checked across every filing that repeats it, a disagreement is reported, and the first published value is the one used.
The cost is that a new company or layout shows nothing until its parser exists; the benefit is that a wrong number is much harder to miss.

**SQLite in a bucket object, not a managed database.** The workload is one batch writer every few hours and a few MB of data (about 0.8 MB
per company). A SQLite file fits that, and the same code runs unchanged on a laptop, in GitHub Actions and on Cloud Run. In the cloud the
update job downloads the file from Cloud Storage, updates it and uploads it only if nobody else uploaded in between (a generation
precondition), so two runs can never overwrite each other; the web page reads a copy. The trade-offs: a single writer and no concurrent
edits, which is fine here and would not be for many users. A managed database (Cloud SQL, Postgres) would cost money every month and add
a service to run for no benefit at this size. How the repository grows, and when to change the storage, is measured in
[docs/database-growth.md](docs/database-growth.md).

**An AI assistant gets the same guardrails.** `valuation-monitor-mcp` is an [MCP server](docs/mcp-server.md) that lets an assistant such as
Claude look up valuations. It is read-only (the database is opened in SQLite's read-only mode and the network is off), every result carries
its sources (filing link, publication time, effective trading day and the written-out calculation), every tool takes an `as_of` date so only
data that was public by then is used, and all arithmetic stays in Python so the model only calls tools and explains the result. The cost is
that the assistant can only answer what the tools offer, in return a wrong or unsourced number is hard to produce and easy to catch.

**Status:** v0.4.0. Project conventions (English-only code and output, tests, how the data is written) are in [CLAUDE.md](CLAUDE.md).

## What it tracks

| Stock | Metrics | Earnings filings |
| --- | --- | --- |
| Tencent Holdings 0700.HK | PE (IFRS), PE (Non-IFRS), PS, PB | Quarterly, `highlights` layout |
| Sunny Optical Technology 2382.HK | PE, PS, PB (only HKFRS data is disclosed, no Non-IFRS) | Half-yearly (interim, annual), `statements` layout |
| JD.com (ADR) JD | PE, PE (Non-GAAP), PS, PB; the price is USD per ADS, EPS is RMB per ADS | Quarterly, 6-K press releases on SEC EDGAR, `press_release` layout |
| Samsung Electronics 005930.KS | PE, PS, PB (price and financials are both in KRW; no Non-GAAP data) | Quarterly, English consolidated financial statements on the Samsung IR website, `kifrs_statements` layout |
| Hikvision 002415.SZ | PE, PS, PB (A-share, RMB; the company discloses no Non-GAAP data) | Quarterly, Chinese periodic reports and annual express reports on CNINFO, `cn_report` layout |


The code is designed around "many stocks x many metrics": to add a stock, edit the config; to add a metric, write a metric class (see "Extending").

## Quick start

Python 3.10 or later is required. Install:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

### Identifying yourself to SEC EDGAR

US company filings come from SEC EDGAR. EDGAR requires every request to say who is making it (a contact in the User-Agent), otherwise it refuses (HTTP 403), and it limits requests to 10 per second (the program uses about 5 per second). The contact is not written in the code; it is provided through an environment variable:

```bash
export SEC_USER_AGENT="valuation-monitor your-email"
```

Without this variable the program does not contact EDGAR and uses the filings already saved in `data/filings/` (newly published filings can only be fetched once it is set). The online site (GitHub Actions) reads the repository secret `SEC_USER_AGENT`: repository Settings -> Secrets and variables -> Actions -> New repository secret, name `SEC_USER_AGENT`, value as above. Never commit it.

### Web interface

```bash
.venv/bin/valuation-monitor-web
```

On start it first updates the data (a few seconds to a dozen or so), then opens http://127.0.0.1:8050/ in the browser. Press Ctrl+C to quit.

- **History selector**: the drop-down at the top right chooses which period the historical percentile is computed over: the past 10, 5, 3, 2 or 1 years (default 10). The percentile and level on the home page, the metric cards on a stock page, and the time range and percentile lines of the trend charts all follow it. The choice is remembered and carried to other pages (the URL gets `?years=5`; share that URL and the other person sees the same choice). The results for every number of years are computed when the page is generated, so the online (static) site can switch too.
- **Home page**: lists all tracked stocks with the latest price, the current value of each metric, its historical percentile and valuation level.
- **Stock page**: click a stock. It shows:
  - the current value and historical percentile of each metric, with a bar marking the current position;
  - a trend chart for each metric (with P10-P90 percentile lines) and a price chart;
  - data sources and cross-check status;
  - download buttons: historical prices, historical earnings, per-share metrics (with formulas), daily valuation detail, all raw data (zip). The CSVs have English column names (file names such as `0700.HK_historical_prices.csv`) and open directly in Excel.
  - **Checking the calculation**: hover over a day on a trend chart to see the full formula of that day's ratio, e.g. `PE = close / (per-share value x FX rate) = 232,000 KRW / (32,308 KRW x 1) = 7.18`, followed by how the per-share value was obtained (`TTM EPS = 2025Q4 2,874* + 2026Q1 7,123 + 2026Q2 10,849 + 2026Q3 11,462 = 32,308`; for PS and PB it also lists the revenue of each period, and how equity and shares were obtained), which period of earnings it came from and when that was published.
- **Update data**: the button at the top right fetches the latest prices and filings over the network.

Options: `--port` sets the port (default 8050), `--no-browser` does not open the browser; `-c`, `--data-dir` and `--refresh` are the same as in the CLI. By default only the local machine may connect.

### Online site (GitHub Pages)

URL: https://yun-zhsh.github.io/Watchlist-monitor/ (works on phones and computers)

GitHub Actions (`.github/workflows/pages.yml`) runs every 4 hours, i.e. at 9:47, 13:47, 17:47, 21:47, 1:47 and 5:47 Hong Kong time (GitHub does not guarantee punctuality; it may be a few minutes to over half an hour late). Each run:

1. updates prices and filings, and commits any change back to the `data/` directory of the repository;
2. exports a static site with the same pages as the local site (without the "Update data" button) and publishes it to GitHub Pages.

A push to `main` also triggers a run. To update immediately, open the repository's Actions page, choose "Update data and publish site" and click "Run workflow".

Notes:

- Before first use, set Source to "GitHub Actions" in repository Settings -> Pages.
- The repository is public, so anyone can open the site.
- If a public repository has no activity for 60 days, GitHub disables scheduled workflows automatically; re-enable them on the Actions page.
- You can also export the static site locally: `.venv/bin/valuation-monitor-export --out site`.

### Running on Google Cloud (dynamic page, only you can open it)

The page can also run on Google Cloud Run: a scheduled Cloud Run job (`valuation-monitor-update`) fetches the data every
4 hours, the page (served with gunicorn, `valuation_monitor.web.wsgi:app`) shows it and lets you start an update with the
"Update data" button, and Identity-Aware Proxy lets only your Google account in. The SQLite database is kept as an object in a
Cloud Storage bucket: the job downloads it, updates it and uploads it back (refusing if someone else uploaded a newer version
in the meantime), and the page reads a downloaded copy (`cloud.py`). The `Dockerfile` builds one image used for both.
Step-by-step instructions are in [docs/deploy-google-cloud.md](docs/deploy-google-cloud.md). It runs side by side with the
GitHub Pages site.

### Command line

```bash
.venv/bin/valuation-monitor --plot charts
```

Example output (values of 2026-11-02; they change with prices; the five stocks are printed in turn, only Tencent is shown here):

```
Tencent Holdings (0700.HK)  Earnings source: HKEXnews, 48 original earnings filings, currency CNY
  ⚠ Non-IFRS EPS 2015Q1 differs between filings (probably a restatement) [0.759, 0.776]; using the first published value 0.759
  Earnings detail  table periods in data/valuation.db
  Price check  Tencent vs Yahoo (after undoing Yahoo's adjustments): Agree 2451 days, Tencent only 10 days (details: table prices_final in data/valuation.db)

Tencent Holdings (0700.HK)  PE (TTM)
  Date        2026-11-02
  Current     12.78
  Percentile  4.7%  ->  Deeply undervalued
  Window      2016-11-02 ~ 2026-11-02 (2460 trading days)
  Range       min 10.0 / mean 28.8 / max 59.7
  Bands       P10=13.9  P30=19.3  P50=25.4  P70=37.2  P90=47.9

Tencent Holdings (0700.HK)  PE Non-IFRS (TTM)
  Current     11.08
  Percentile  0.3%  ->  Deeply undervalued
  ...

Tencent Holdings (0700.HK)  PS (TTM)
  Current     3.81
  Percentile  0.9%  ->  Deeply undervalued
  ...

Tencent Holdings (0700.HK)  PB
  Current     2.64
  Percentile  0.6%  ->  Deeply undervalued
  ...
```

Options:

- `-c`: path of the config file.
- `--plot DIR`: write the trend charts.
- `--data-dir`: directory where data is saved (default `data`).
- `--refresh`: ignore the 12-hour network request cache (the `.cache/` directory).

## Using it from an AI assistant (MCP server)

```bash
pip install -e '.[mcp]'
claude mcp add valuation-monitor -- valuation-monitor-mcp --data-dir /path/to/Watchlist-monitor/data
```

Then ask, for example, *"Where does Samsung's PB stand over the last ten years?"*. The assistant calls `get_valuation`, and the answer
rests on a result like this (real output from the saved data, shortened):

```
PB = close / (per-share value x FX rate) = 229,000 KRW / (91,301.3691 KRW x 1) = 2.5082
percentile 92.6 of 2,429 trading days (2016-11-02 to 2026-11-02), level "Deeply overvalued"
earnings used: 2026Q3, published 2026-10-29 10:00 (Asia/Seoul), in effect from 2026-10-29
source: https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2026_3Q_conference_eng.pdf
warning: the latest earnings used are preliminary (earnings call presentation, not yet the audited statements)
```

With `as_of="2025-03-31"` the same question gives PB 1.00 at the 4.1th percentile, from the 2024 Q4 statements published on 2025-01-31: nothing
published later is used (where a later document replaced a first release, the answer says so). The four rules (read-only, sourced, point in time, no model arithmetic), the tools, the setup for Claude Desktop and
Claude Code, running it as a **public read-only Cloud Run service** (`scripts/deploy-cloud.sh mcp`, so an assistant anywhere can use it by a
URL; rate-limited, a service account that can only read the bucket), and the limits are in [docs/mcp-server.md](docs/mcp-server.md).

## Saved data

Everything the program saves lives in `data/` and is committed to the repository with the code: one **SQLite database**, `data/valuation.db`, for prices, FX rates, parsed filings and earnings, plus the original filings as files. Later runs only add what is new; when the network is down the saved data is used.

| Where | Content | Updated by |
| --- | --- | --- |
| `data/filings/0700.HK/*.pdf`, `index.json` | Original earnings announcement PDFs (49 since 2014, about 20MB); the index records title, publication time and source link | Only newly published announcements are downloaded |
| `data/filings/JD/*.htm`, `index.json` | Original JD earnings press release HTML (49 since 2014, about 23MB); the index records every 6-K that was checked (including non-earnings ones) so they are not checked again | Only newly filed 6-Ks are checked and downloaded |
| `data/filings/2382.HK/*.pdf`, `index.json` | Original Sunny Optical earnings announcement PDFs (24 since 2014); same index | Only newly published announcements are downloaded |
| `data/filings/005930.KS/*.pdf`, `index.json` | Excerpts of Samsung's English financial statements: consolidated statement of income (`soi`), of financial position (`bs`), and the EPS note page (`*_eps.pdf`); while the latest quarter's statements are not out, the earnings call material is kept temporarily; the index records the earnings call time | Only newly published quarters are downloaded |
| `data/filings/002415.SZ/*.pdf`, `index.json` | Relevant pages of Hikvision's original Chinese periodic and express reports (about 59 since 2014, about 29MB); the index records publication time, the basis for it and the source link (titles in the index are English) | Only newly published filings are downloaded |
| `data/valuation.db`, table `price_bars` | Daily bars of each source and symbol: Tencent (open, high, low, close, volume, **unadjusted actual traded prices**; HK stocks only) and Yahoo (close ex-rights adjusted, adjclose further adjusted for dividends) | Only trading days after the last saved day are inserted; when Yahoo adjusts history retroactively the series is downloaded again |
| table `fx_rates` | Daily FX rates of RMB to HKD and USD (USD is for JD) | Only new trading days are inserted |
| table `prices_final` | **The final closing price decided from all sources**, plus source, cross-check status, the price of each source and the Yahoo adjustment factor | Decided again on every run; only changed days are written |
| table `meta` | One JSON document per series: source note, where the download starts, currency, split records | Updated with the series |
| table `announcements` | Values parsed from each filing as stated: revenue, net profit attributable to owners, basic EPS, Non-IFRS/Non-GAAP basic EPS, equity at period end, shares disclosed in the filing, plus whether the data is preliminary, the basis of the share count and a publication time note | Only new filings are parsed |
| table `periods` | Per-period (quarter; half year for half-yearly reporters) revenue, net profit attributable to owners, EPS, equity at period end, weighted average shares disclosed in the filing (`wa_shares`), implied shares (for cross-checking), with first publication time, effective trading day, number of confirming filings, largest deviation and source link | Recomputed from `announcements` on every run; rewritten only if it changed |
| table `ttm` | The per-share data used for valuation after each filing, **with the full calculation**: EPS and revenue of each period the TTM covers (periods 1-4), the TTM total, latest net profit and basic EPS, shares, revenue per share, equity at period end, book value per share, and the formula text of every step; also publication time, available date, effective trading day, whether the data is preliminary and the split adjustment note | Same |

Notes:

- **Looking at the data**: any SQLite tool opens `data/valuation.db` (for example `sqlite3 data/valuation.db "select * from prices_final where symbol = '0700.HK' order by date desc limit 5"`). The page's download buttons give CSV files, and `python -m valuation_monitor.legacy export-csv DIR` (also installed as `valuation-monitor-db export-csv DIR`) writes the whole database as CSV and JSON files (`prices/<symbol>/...`, `fx/...`, `eps/<symbol>_<table>.csv`), the layout used before the database existed. `import-csv DIR` reads such a folder back; the round trip is byte for byte.
- **Why a binary file in git**: the database is small (a few MB for five companies) and only changed rows are written, so the repository grows slowly. How fast it grows, what the original filings add, and when to review and change the approach are in [docs/database-growth.md](docs/database-growth.md). The program never rebuilds or vacuums the file, and the scheduled workflow fails if it grows over 50MB, as a reminder to stop keeping it in git. Do not commit it from a local run: only the scheduled workflow does, and a binary file cannot be merged. If `git pull` complains about local changes to it, `git checkout data/valuation.db` drops them.
- Raw traded prices and FX rates are never changed once saved. On every incremental download the program compares the last 7 saved days with the source and gives a notice if the source changed its history.
- Yahoo's adjusted prices change retroactively with new ex-rights events by nature; when a change is found the whole series is downloaded again (older versions of the database are still in git history).
- Today's prices can still change before the close, so they take part in the calculation but are not saved. Whether a day has "closed" is decided in each source's own timezone: Hong Kong time for HK stocks, London time for Yahoo FX.
- After the parsing rules are improved (add 1 to the number of the layout in `PARSER_VERSIONS` in `filings.py`) the saved parse results are invalidated automatically and parsed again.
- Other features can read the database directly; every table has a `symbol` (or `pair`) column that tells the stocks apart.
- The schema version is kept in SQLite's `user_version`; a database written by a newer version of the program is refused instead of being damaged.

## How to check the calculation

Every number can be traced back to the raw data step by step; the pages and the download files use the same formula text (`audit.py`):

1. **Chart tooltip** (cursor on a day): ratio = close / (per-share value x FX rate), with that day's close, FX rate and per-share value substituted; the per-share value is expanded into the sum of the TTM periods (or TTM revenue / shares, equity / shares; shares are taken from the figure disclosed in the filing first and only implied from net profit / basic EPS if none is disclosed, see "Shares").
2. **Daily valuation detail** (`valuation.csv`): for each trading day and each metric, one row with the close, price source, FX rate, which period of earnings was used that day (reporting period and publication time), the per-share value (reporting currency and converted) and the ratio. The ratio column = close / converted per-share value, which can be checked directly in Excel.
3. **Per-share metrics** (`ttm.csv`): one row per filing; after finding the period in the daily valuation detail, this lists the value of each period the TTM covers (periods 1-4), the total and the formula. For example Samsung 2026Q3: `TTM EPS = 2025Q4 2,874* + 2026Q1 7,123 + 2026Q2 10,849 + 2026Q3 11,462 = 32,308`, where `*` means the period was obtained by subtracting cumulative figures (Q4 = full year - first three quarters).
4. **Historical earnings** (`eps.csv`): the value of each period, first publication time, number of confirming filings and the source link; the values as stated in the filings are in the `announcements` table, and the values before and after split adjustment are compared in the "split adjustment note" column of `ttm.csv`.

## Method

1. For each trading day, PE = close / (TTM EPS x FX rate). TTM EPS = the sum of the single-quarter basic EPS of the last 4 quarters.
2. Take the PE series of the last N years (10, 5, 3, 2 or 1 on the web page, default 10) and compute the **historical percentile** of the current value (the share of trading days in that period whose PE is <= the current value). A shorter window reflects the recent valuation level, a longer one the long-term level. The same current value can have very different percentiles in different windows, e.g. Sunny's PB on 2026-10-29 is at 6.7% over the past 10 years (Deeply undervalued) and 34.1% over the past year (Fair). The selectable years are set by `lookback_options` in `config.yaml`; `lookback_years` is the one selected by default.
3. The percentile is mapped to a level by `levels` in `config.yaml`: <=10% Deeply undervalued, <=30% Undervalued, <=70% Fair, <=90% Overvalued, otherwise Deeply overvalued (level names are English and set in `config.yaml`).

Five metrics are provided:

| Metric | Calculation |
| --- | --- |
| `pe`: PE (TTM) | Price / sum of the basic EPS of the last 4 quarters (IFRS basis). It is affected by one-off items, e.g. Tencent's Q4 2021 and Q4 2022 had large gains from distributing JD and Meituan shares, which made TTM EPS swing widely. |
| `pe_non_ifrs`: PE Non-IFRS (TTM) | Same, using the Non-IFRS basic EPS Tencent discloses, which excludes investment gains, share-based compensation etc. and reflects the earning power of the core business better. |
| `pe_non_gaap`: PE Non-GAAP (TTM) | The equivalent for US-listed companies (JD): uses the basic Non-GAAP EPS (per ADS) the company discloses, excluding share-based compensation, investment gains or losses etc. |
| `ps`: PS (TTM) | Price / revenue per share. Revenue per share = sum of revenue of the last 4 quarters / shares. |
| `pb`: PB | Price / book value per share. Book value per share = equity attributable to owners at the end of the latest period / shares. Not computed when equity is negative. |

**Shares**: if the filing discloses a number it is used; only if it does not is it implied. For HK, US and Korean stocks the **weighted average shares used for basic EPS** of the latest period is used (the same basis as EPS); the quarterly reports of A-shares do not disclose weighted average shares, so **period-end total shares** are used (last row of the table):

| Stock | Source of shares | Unit and notes |
| --- | --- | --- |
| Tencent | "Weighted average number of ordinary shares in issue" in the EPS note of the filing (excluding share award scheme and treasury shares) | Millions of shares, the three-month column; the Q4 filing lists only the full-year figure, so the period's shares are derived from it (see below) |
| Sunny | "Weighted average number of ordinary shares for the purpose of basic earnings per share" in the EPS note | Thousands converted to millions; the interim report is the first half, the annual report the full year (stated in the formula) |
| JD | "Weighted average number of shares: Basic" in the press release income statement | Thousands or millions (by the table unit) / 2 (1 ADS = 2 shares), giving millions of ADS |
| Samsung | Sum of the ordinary and preferred share weighted average shares in the "Earnings per Share" note of the English financial statements | The note unit is thousands of shares or single shares, decided by the magnitude of net profit / EPS; only this page of the original is saved (`data/filings/005930.KS/*_eps.pdf`); Q4 has only the full-year figure, derived the same way (see below) |
| Hikvision | "Share capital" on the balance sheet (period-end total shares, par value RMB 1, so shares = the share capital amount, including treasury shares) | Yuan converted to millions of shares; present in quarterly, half-year, annual and express reports, so Q4 does not need to be derived |

Shares are implied (shares = net profit attributable to owners of the latest period / basic EPS) only in these cases: the latest Samsung quarter whose English statements are not out yet (the call material is used and has no note); the first three quarters' shares needed to derive a Q4 are missing; a future company whose filings have no share count. **Q4**: the annual reports of Tencent, Sunny (second half) and Samsung disclose only the full-year weighted average shares, not a separate Q4 (second half) figure. The full-year figure is the day-weighted average of the quarters, so Q4 shares = (full-year figure x days in the year - each of the first three quarters' shares x its days) / days in Q4. For example Tencent 2025Q4: (9,085 x 365 - 9,106 x 90 - 9,097 x 91 - 9,081 x 92) / 92 = 9,056.6. Using the full-year figure as the Q4 share count would be less accurate (when a company keeps buying back shares the full-year average is higher than Q4's; for Tencent that quarter the gap is 0.3%), so it is not done. Only when the first three quarters' shares are missing does it fall back to implied.

The disclosed (or derived from the full year) shares and the implied cross-check value usually differ by less than 0.1%, at most 0.3% (JD's early loss-making years have a tiny EPS, so the implied value itself is imprecise, up to 1%).

Strictly speaking PS and PB should use period-end shares; when a company keeps buying back shares, period-end shares differ from the period's weighted average (Tencent usually by less than 0.3%).

Per-share data is computed in the reporting currency and then converted to the price currency at that day's FX rate: Tencent and Sunny from RMB to HKD; JD from RMB to USD (per ADS); Samsung (KRW) and Hikvision (RMB) have the same reporting and price currency, so the rate is 1.

## Data sources

| Data | Source |
| --- | --- |
| Earnings per share (IFRS/GAAP, Non-IFRS/Non-GAAP) | HK: **the original earnings announcement PDFs on HKEXnews**; US: **the original earnings press release HTML in the 6-K exhibits on SEC EDGAR**; Korea (Samsung Electronics): **the original English consolidated financial statement PDFs on the company IR website**; A-share (Hikvision): **the original Chinese periodic report and express report PDFs on CNINFO (the information disclosure platform designated by the CSRC)**. The program parses them itself; no third-party data vendor is involved |
| Filing publication time | HKEXnews records it to the minute; EDGAR's acceptance time (`acceptanceDateTime`, UTC, converted to New York time); Samsung: the date and time (Korea time) of the Earnings Conference Call on the IR "Events" page; Hikvision: the PDF upload time since October 2020, only the listing date before that (see "`cn_report` layout") |
| Daily prices | HK: Tencent quote API (unadjusted) and Yahoo Finance (ex-rights adjusted), both saved and cross-checked; US, Korea, A-share: Yahoo Finance only (Tencent has no usable API for them), so there is no second source to cross-check |
| Split records, FX rates | Yahoo Finance |

### How the final price is decided (HK)

HK stocks have two sources (Tencent, Yahoo) and one must be chosen; US, Korean and A-share stocks have only Yahoo, whose close is used directly (it is already split-adjusted; zero-volume days are invalid). A historical PE needs the **actual traded price** of the time. The two sources behave differently:

- Tencent gives the unadjusted actual traded price.
- Yahoo's close treats distributions in kind as ex-rights events and lowers historical prices retroactively. After Tencent distributed JD shares (ex-date 2022-01-20) and Meituan shares (2023-01-05), Yahoo multiplied the prices before 2022-01-20 by 0.9219 and those from 2022-01-20 to 2023-01-04 by 0.9498. Yahoo also generates zero-volume rows with a wrong close on typhoon closure days and half days.

The combination rules:

1. Zero-volume rows are invalid.
2. Estimate Yahoo's daily adjustment factor from the ratio of the two sources. A day's ratio is accepted as soon as it equals that of the previous or next trading day. This also gives the correct factor on the ex-rights date itself, and a few outlier days do not affect the result.
3. Undo Yahoo's adjustment and compare with Tencent's price; a difference of at most 0.2% counts as agreement.
4. The final price is Tencent's; the unadjusted Yahoo price is used only on days Tencent is missing. On disagreement Tencent's price is still used and marked `mismatch` in the `prices_final` table for manual review.

Current cross-check result (Tencent): since 2015-10 there are 2709 trading days; the two sources agree on 2699, and the other 10 are half days (invalid Yahoo data); there are no disagreeing days.

Yahoo's `adjclose` (further adjusted for dividends) can be used later to compute total returns including dividends.

### How earnings data is extracted from filings

The layout of the filings differs by company, so the program has one parse function per layout, selected by `filing_format` in `config.yaml`. What is extracted is always: revenue, net profit attributable to owners, basic EPS, equity attributable to owners at period end, the shares disclosed in the filing (and the corresponding basic EPS for companies with Non-IFRS/Non-GAAP figures). There are five layouts: `highlights` (Tencent), `statements` (Sunny), `press_release` (JD), `kifrs_statements` (Samsung), `cn_report` (Hikvision); see the sections below.

**`highlights` layout (Tencent)**: reads four rows of the financial highlights table on page 1: "Revenues", "Profit attributable to equity holders of the Company", "EPS (RMB per share) - basic" and "Non-IFRS EPS (RMB per share) - basic":

- **Quarterly format** (from Q3 2016): one row has three numbers: this quarter, same quarter last year, previous quarter.
- **Cumulative format** (earlier interim, Q3 and annual announcements): one row has two numbers: this year to date, same period last year to date. Single quarters are obtained by subtracting the earlier quarters of the year from the cumulative figure.

**`statements` layout (Sunny Optical)**: the announcement has no highlights table; the program reads "Revenue", "Profit for the period/year attributable to: Owners of the Company" and "Earnings per share - Basic (RMB cents)" from the consolidated income statement, each row with two numbers: this period to date, same period last year to date. Amounts are in thousands and EPS in RMB cents; they are converted to millions and yuan. The note number column of the income statement has several spellings (`3`, `3A`, `3,4`), all recognised. Next to the net profit attributable to owners there is a "total comprehensive income" row written the same way, so net profit must be inside the income statement area and preceded by the label "Profit for the period/year"; if it is not recognised an error is raised, and another row is never taken.

Both layouts read the "Equity attributable to ... of the Company" subtotal from the balance sheet; the two numbers are the end of this period and the end of last year.

**Quarterly or half-yearly reporting** is decided automatically from the existing filings: a company with Q1 or Q3 filings reports quarterly; one with only interim (Q2) and annual filings reports half-yearly. For half-yearly reporters the first-half data is taken directly from the interim announcement, the second half = full year - first half, and the TTM is the sum of the last two half years. For example Sunny's TTM EPS at the end of June 2026 = second half of 2025 (4.2655 - 1.5156) + first half of 2026 1.6803 = 4.4302.

### How accuracy is ensured

- **Cross-checks**: the revenue, net profit and EPS of a period appear in several filings (the filing itself, and the "previous period" and "same period last year" columns of later filings); the equity at each year end appears in that year's annual report and in the "end of last year" column of every filing of the next year. The same holds for values derived from cumulative figures: the same-period-last-year cumulative figure in a next-year filing also yields the value of that period, as another source. The program compares them all, gives a notice on disagreement, and uses the first published value (what the market saw at the time).
  - The current disagreements are all Tencent's Non-IFRS (then called Non-GAAP) EPS of 2014-2015: in 2015 Tencent changed the basis and restated the 2014 comparatives, e.g. the 2014 full-year Non-GAAP EPS from 2.624 to 2.680. A restated cumulative figure minus the unrestated earlier periods gives a derived value (marked "derived from cumulative figures") that deviates by the size of the restatement; this only signals a restatement, and the derived value is not a true quarterly figure on either basis.
  - Tencent's revenue, net profit and equity, and all of Sunny's data, agree across filings and raise no notices.
  - The program also checks the shares of adjacent periods and gives a notice when they change by more than 5%, to catch extraction errors; the disclosed shares and the implied shares are compared with each other (see "Shares").
- **Independent verification**: the parse results of Sunny's 24 filings were checked against the revenue and net profit in the narrative text on page 1 of each filing (e.g. "profit ... attributable to owners of the Company was approximately RMB1,808.4 million"); all 12 agree. The implied weighted average shares agree with those disclosed in the note (e.g. 1,076.2 million for the first half of 2026, the note says 1,076,196 thousand).
- **Annual check**: for quarterly reporters the four single-quarter EPS add up to the full-year EPS disclosed in the annual report (e.g. Tencent 2015: 0.741 + 0.787 + 0.800 + 0.769 = 3.097). For half-yearly reporters the second half = full year - first half, which holds by construction and relies on the cross-checks above.
- **Manual checking**: in the `periods` table every period lists the value, first publication time, effective trading day, number of confirming filings, largest deviation and source link.
- **No peeking at future data**: each EPS value is only used after its filing is published; the exact rules are in the next section.
- **Currency and splits**: the currency is read from the filing text ("RMB per share"). If a split happened, both the pre-split EPS and the price are converted to the current share basis using the split records (the saved prices remain actual traded prices); revenue, net profit and equity are totals and unaffected by splits; shares (disclosed and implied) are multiplied by the split ratio, on the same basis as EPS.

**`press_release` layout (JD, US)**: the tables in the press release are laid out as "header + data rows": the header has group descriptions (For the three months ended...), a date row and a currency row (RMB, RMB, US$), and each group ends with a US dollar column. The program takes the RMB columns of the "three months" group, told apart by date (this quarter, same quarter last year, and in some years the previous quarter; the column order differs between years, so it goes by date rather than position), and reads:

- revenue `Total net revenues`;
- net profit attributable to ordinary shareholders. In years with discontinued operations (2017-2018, the JD Finance spin-off) the income statement splits into "continuing" and "discontinued" operations, and only the total is taken;
- basic EPS per ADS (RMB; 1 ADS = 2 ordinary shares). JD's 2014 net loss is further reduced by the preferred share accretion, and the basis is the "attributable to permanent equity holders" figure in the filing;
- basic "Non-GAAP per ADS" EPS (written as Adjusted in 2014);
- `Total JD.com, Inc. shareholders' equity` from the balance sheet (end of this period, end of last year; the total is not taken when there is a non-controlling interest row).

Units (thousands or millions) are in the note before the table and are converted to millions. When the income statement is split by a page break into two tables, the second half without a header (per-share data) reuses the previous table's header and is only used for the EPS rows. When the same value appears in several tables it must agree; otherwise an error is raised and one of them is never silently picked. Shares are the weighted average shares disclosed in the press release (thousands or millions, by the table unit) divided by 2, giving millions of ADS, stable at 1.2-1.6 billion, usually within 0.1% of the "net profit / basic EPS" implied value.

JD verification: all 49 press releases were parsed successfully; for 2017-2025 the quarterly revenue and net profit of each year add up exactly to the full-year figures of the 20-F annual reports (XBRL), and the year-end equity also matches the 20-F. 2015-2016 show disagreements, which are restatements after the JD Finance spin-off; the program applies the "first published value" rule and gives a notice (visible on the page).

**`kifrs_statements` layout (Samsung Electronics, Korea)**: the Financial Information page of the Samsung IR website offers English consolidated financial statements (K-IFRS, reviewed or audited by the accountants, 2010 to date) by quarter. Two files are read each quarter, both excerpts of the original statements: the consolidated statement of income (`soi`) and of financial position (`bs`), saved in `data/filings/005930.KS/`.

- For Q1-Q3 the first two columns of "three months" of the income statement are used (this quarter, same quarter last year); for Q4 only full-year statements exist, so the full year (this year, last year) is used and the single quarter is obtained by the program as full year minus the first three quarters, so the Q4 TTM EPS equals the annual report's full-year EPS exactly (e.g. KRW 6,605 for 2025, KRW 4,950 for 2024).
- Revenue is Revenue, net profit is Profit attributable to owners of the parent, EPS is basic EPS of ordinary shares (KRW); equity = Total equity - Non-controlling interests. Amounts are in millions of KRW and the price currency is also KRW, so no FX rate is needed.
- PDF text extraction inserts spaces after thousands separators (e.g. "44, 260,956") and control characters; the program repairs these first. Samsung has ordinary and preferred shares and net profit includes the part attributable to preferred shares, so the shares are the sum of the ordinary and preferred weighted average shares in the note (about 6.5-7.5 billion; PS and PB use this total, which differs slightly from the ordinary-share price basis, as the preferred share price is usually lower). In some files the note is in thousands of shares and in others in shares, and the PDF sometimes splits numbers (e.g. "5,969,78 3"); the program repairs them, then checks the magnitude against "net profit / EPS", and if it does not match the note is not used and the shares are implied instead.
- Samsung split 50-for-1 on 4 May 2018; the EPS in earlier filings (e.g. KRW 85,435 for Q1 2018) is divided by 50 using Yahoo's split record; Yahoo's close is already split-adjusted.
- **The latest quarter uses the earnings call material first**: the English statements come about three weeks after the call (about six weeks for Q4), so until they are out the program uses the numbers in the English earnings presentation published on the day of the call (IR website, Earnings Release page): revenue, net profit attributable to owners (precise only to 0.1 trillion KRW), ordinary-share EPS (exact KRW) and equity (the material shows total equity including non-controlling interests, scaled by the previous quarter's "attributable / total" ratio, so it is an estimate). Once the statements are out, the program switches to the statement numbers automatically; the publication time stays the day of the call. The "Data sources and checks" section of the page notes which quarter still uses preliminary data. The material layout changes; currently supported are the layout since 2026 for Q1-Q3 (three columns: same quarter last year, previous quarter, this quarter) and the layout since 2024 for Q4 (two extra columns: last year and this year full year); if it cannot be parsed it is skipped with a notice and the program waits for the statements, never producing wrong numbers. The same-quarter-last-year and previous-quarter EPS in the material are also cross-checked against the statements (e.g. KRW 1,802 and 10,849 in the 3Q26 material match the statements). The Q4 material is read as full-year figures (full-year revenue, net profit and EPS, handled like the annual report as full-year cumulative figures, with the single quarter obtained as full year minus the first three quarters), so the Q4 TTM EPS equals the annual report's full-year EPS exactly and does not jump once the statements are out (checked with the Q4 materials of 2024 and 2025: full-year EPS of KRW 4,950 and 6,605 match the annual reports; revenue and net profit are within 0.1 trillion KRW; the equity estimate is within 0.03% of the annual report).
- Publication time: the English statement PDFs carry no publication time, and the English version is uploaded about three weeks after the results first became public, so the time of the earnings call on the IR Events page is used (e.g. "3Q26 Earnings Conference Call, October 29, 2026, 10:00 a.m. KST", recorded for every quarter since 2009), when the market first saw the numbers; up to 2014 only the date is given and 10:00 is assumed. The Korea Exchange closes at 15:30 and the calls are all in the morning, so the effective trading day is the day of the call. The numbers in the call are unreviewed, while the program reads the reviewed statements published later; where they differ the statements win. Checked with Q2 2026: the EPS in the call material (KRW 10,849, 7,123, 737) matches the statements.
- Verification: all 48 statements (Q3 2014 to Q2 2026) were parsed successfully, the Q4 TTM EPS of each year equals the annual report's full-year EPS, and repeated disclosures of the same value in adjacent filings agree (the rounding error after split adjustment is within the tolerance).

**`cn_report` layout (Hikvision, A-share)**: CNINFO has all of Hikvision's Chinese periodic reports since 2014 (Q1 report, half-year report, Q3 report, annual report) and annual express reports. The program downloads the original PDFs and keeps only the pages it uses (the key accounting data table, the consolidated income statement, the consolidated balance sheet, pages unchanged). The Chinese-language patterns used to recognise titles and text are in `cn_patterns.py`; everything the program shows is English (titles such as "2026 Q3 report", "2025 annual express report").

- Q1: the current and prior period of the consolidated income statement (single quarter). Q2 (half-year report) and Q4 (annual report): the current and prior period of the consolidated income statement, which are cumulative, and the single quarter is obtained by subtraction. Q3: the consolidated income statement is year to date, so the single quarter is taken from the first column (the reporting period) of the key accounting data table. Verification: for Q3 reports since 2021, "year to date - half-year report" equals this column exactly (zero difference for both revenue and net profit).
- Revenue is total operating revenue, net profit is net profit attributable to shareholders of the parent, EPS is basic EPS (yuan per share), equity is total equity attributable to owners of the parent; amounts are in yuan and converted to millions. In every report the key data table and the consolidated income statement must agree on the same item, otherwise an error is raised.
- Shares are the "share capital" of the balance sheet (period-end total shares, par value RMB 1, so shares = the share capital amount). A-share quarterly reports do not disclose weighted average shares, so unlike the HK and US stocks, period-end total shares are used here; they include treasury shares and are therefore 1%-2% higher than the share count behind EPS (the implied value is listed in the per-share metrics file for comparison). Computing PS and PB with total shares is equivalent to "total market cap / revenue" and "total market cap / equity", the usual definitions.
- Hikvision's EPS is adjusted for the two bonus-share distributions of 2016 and 2017 that Yahoo records (1.5-for-1 each), and the shares are multiplied by the split ratio in the same way.
- **Express reports**: the annual express report is published before the annual report (January-February) and is the first time the full-year results are public; the annual report comes in April. After an express report is out, the program first uses its full-year data (revenue, net profit, EPS, equity, share capital); once the annual report is out, it is replaced by the annual report's exact numbers, but the publication time stays that of the express report (the market first saw the full-year results that day). The express and annual numbers differ slightly (2014-2025: revenue within 0.3%, net profit within 0.5%, EPS by no more than 0.01 yuan, equity mostly within 0.15%); the annual report wins; the express report is not audited by the accounting firm and uses preliminary internally audited numbers. Between the express report and the annual report the "Data sources and checks" section of the page notes that the year uses express report data.
- **Publication time**: the CNINFO announcement list has only dates, and records filings made after the close at 00:00 of the next day (Hikvision usually publishes after the close on Friday, shown as Saturday 00:00 in the list), so it is treated as "before the open that day", and the effective trading day is that day or the first trading day after it. Since October 2020 the upload time of the PDF is the actual publication time (Friday 16:00-22:00) and is used directly; earlier files were migrated in September 2020, so their upload time is meaningless and is not used.
- **Chinese and English versions**: CNINFO has English versions of the reports since 2016, but not for 2014-2015 and some quarters, so the complete Chinese originals are used; the English versions can be a cross-check source later (see "To do"). Hikvision's own IR website has access restrictions (JavaScript verification) and is not used.

### From which day an EPS is used

1. **Publication time**: the publication time recorded by the source is used (to the minute for HKEXnews, in the exchange's local time).
2. **Available date**: the earliest day whose closing price may already reflect the filing.
   - Published before the close (pre-market, intraday, lunch break): the available date is that day;
   - Published after the close: the available date is the next day.
   - The closing time follows each market's rules (see `market_calendar.py`): A-shares close at 15:00 (Beijing time), no half days; HK full days close at 16:00, and on Christmas Eve, New Year's Eve and Lunar New Year's Eve, if the market is open, only the morning session is held and it closes at 12:00; US stocks close at 16:00 (New York time), with an early 13:00 close on the day after Thanksgiving, 3 July and Christmas Eve (Monday-Thursday); Korean stocks close at 15:30 (Seoul time), no half days. US publication times are also New York local time, with daylight saving handled automatically.
3. **Effective trading day**: the first trading day on or after the available date; from that day the new EPS is used in the PE. Trading days are decided by the price data that actually traded, so weekends, public holidays and typhoon or rainstorm closures roll over automatically. For example Tencent's Q1 2024 results were published on 05-14 (Tuesday) at 16:30 and 05-15 was a holiday (Buddha's Birthday), so they are used from 05-16.
4. The effective trading day is empty in two cases: the filing is earlier than the start of the price data, or the next trading day has not come yet (e.g. a filing published just after today's close).

Of JD's 49 earnings filings, 6 were published just after 16:00 New York time (after the close, effective from the next trading day; e.g. published Friday 2019-05-10 16:10, effective from Monday 05-13), and the other 43 were published before the open (about 6:00-9:30) and are effective the same day.

All of Tencent's earnings announcements since 2014 were published between 16:00 and 18:40, i.e. after the close. The half-day rule was checked against the price data: of the days with zero Yahoo volume, 3 were full-day typhoon closures and the other 10 all match the half days derived from the rules.

## Known limitations

- For the markets that have only Yahoo as a price source (US, Korea, China A-shares), the newest trading day can arrive late: after the close, Yahoo creates that day's daily bar with an empty close, sometimes for many hours. The program skips a bar without a close (it never saves incomplete data) and fills the day in on a later run, so those stocks can lag the real close by about half a day to a day. Hong Kong stocks are not affected, because Tencent's quote API is a second source.
- The parsing rules were written for the filing layouts of Tencent (`highlights`), Sunny Optical (`statements`), JD (`press_release`), Samsung (`kifrs_statements`) and Hikvision (`cn_report`), and verified on all of their filings since 2014. Other companies' layouts may differ and need a new parse function, or a check of whether an existing layout applies (see "Extending").
- The TTM from adding 4 single-quarter EPS differs slightly from "TTM profit / current shares" (Tencent keeps buying back shares, and the two differ by about 1%).
- Only the "own historical percentile" is used; changes in the interest rate environment and growth expectations are not considered, and there is no comparison with peers.
- Prices, FX rates and split records come from Tencent and Yahoo Finance, neither of which is official exchange data. HK prices are cross-checked between two sources; the prices of the US (JD), Korean (Samsung) and A-share (Hikvision) stocks have Yahoo as the only source, and FX rates and split records also have Yahoo as the only source.
- JD's PE is meaningless (blank) during the loss-making years 2014-2017, so the valid range of its historical percentile is shorter (PE from 2018, Non-GAAP PE from 2017); in periods of very small profit the PE reaches several hundred times and stretches the upper end of the historical distribution, which should be kept in mind when reading the percentile.
- Samsung's English financial statements are uploaded about three weeks after the earnings call (e.g. Q2 2026: call on 30 July, English statements on 19 August; Q4 is about six weeks later, e.g. Q4 2025: call on 29 January, annual report on 12 March, and the call material is likewise used in between). In this gap the preliminary data of the call material is used (see above), with revenue and net profit at lower precision and equity estimated. The Korean-language pages of the Samsung IR website (samsung.com/sec/ir) also provide Korean quarterly reports, but they are uploaded on the same day as the English statements (e.g. both on 19 August for Q2 2026), not earlier, so they cannot shorten this lag; for connecting DART see "To do".
- Hikvision's quarterly reports do not disclose weighted average shares, so period-end total shares are used (see "`cn_report` layout"); the 2014-2015 filings have only a date and no publication time and are treated as "before the open that day"; if the real publication was intraday the effective date may be one trading day later than the real one, never earlier.
- If the press release layout of another US company differs from JD's (table labels, location of the summary), the `press_release` parser may need extra rules; if it cannot parse, it raises an error directly and never produces wrong numbers.

## Project structure

```
config.yaml                        watch list, lookback years (default and selectable), valuation levels
CLAUDE.md                          working rules for Claude Code sessions (language rules)
src/valuation_monitor/
  cli.py                           command line entry points (valuation-monitor, valuation-monitor-update, valuation-monitor-check, valuation-monitor-db; the MCP server is in mcp_server.py)
  config.py                        config parsing
  data.py                          gathers the data sources into what the metric calculations need
  db.py                            the SQLite database: tables, reading and writing only what changed
  freshness.py                     checks how old the newest saved price, FX rate and filing is (limits in config.yaml); fails the update when prices or FX rates are stale
  mcp_server.py                    the MCP server: read-only, offline, sourced, point-in-time lookups for AI assistants (docs/mcp-server.md)
  mcp_http.py                      serving it over HTTP on Cloud Run: rate limit, POST only, request log
  cloud.py                         keeps the database in a Google Cloud Storage object (pull, push with a version check, start the update job)
  legacy.py                        import and export in the CSV/JSON layout used before the database (also the raw data download)
  store.py                         saving prices and earnings in the database, incremental updates
  prices.py                        decides the final price from several sources
  filings.py                       extracts revenue, net profit, EPS and equity from filings (parse functions per layout), cross-checks, computes TTM and per-share data
  market_calendar.py               closing times, half days, effective trading day of filings (HK, US, Korea, A-share)
  parsed.py                        common structure of the parse results of all layouts
  audit.py                         writes the calculation of a metric as text (shared by tooltips and downloads)
  press_release.py                 extracts earnings data from US earnings press releases (HTML)
  kifrs.py                         extracts earnings data from Samsung's English consolidated financial statements (K-IFRS)
  cn_report.py                     extracts earnings data from A-share Chinese periodic and express reports
  cn_patterns.py                   the Chinese patterns used to read A-share filings (the only module with Chinese text)
  providers/                       data sources (hkex.py HKEXnews, sec.py SEC EDGAR, samsung.py Samsung IR website, cninfo.py CNINFO, tencent.py prices, yahoo.py prices/splits/FX, http.py cache/retry/rate limit)
  metrics/                         metrics (base.py defines the interface and the daily calculation detail, multiples.py implements PE, Non-IFRS/Non-GAAP PE, PS, PB)
  analysis.py                      historical percentile and level
  report.py                        text report and trend charts (CLI)
  service.py                       fetches data and computes the metrics (shared by CLI and web)
  web/                             web interface (Flask; wsgi.py is the server entry point for Cloud Run): page templates, styles, Chart.js, chart tooltips, download file generation (including daily valuation detail), static site export
data/                              valuation.db (prices, FX rates, parsed filings, earnings) and the saved original filings (see "Saved data")
.github/workflows/pages.yml        scheduled data update and GitHub Pages publication
Dockerfile, docs/                  container image and the Google Cloud deployment guide
tests/                             unit tests (offline, no network access)
```

## Extending

- **Add a Hong Kong stock**: add an entry under `stocks` in `config.yaml` (symbol, English name, `filing_format`, `metrics`) and run once; the program downloads all earnings announcements from HKEXnews automatically. **Always check the data the first time**: look for parse errors; check in the `periods` table of `data/valuation.db` that revenue, net profit and shares look reasonable (they should be stable, and the disclosed shares should be close to the implied cross-check value); check a few numbers against the narrative text on page 1 of the announcement. If the company's announcement layout differs from both existing HK layouts, add a parse function to `FORMATS` in `filings.py`.
- **New metric**: extract the extra data from the filings in `filings.py`, implement the metric class under `metrics/` (metrics of the "price / per-share figure" kind can inherit `PriceMultiple` directly), then register it in `metrics/__init__.py`.
- **Add a US stock** (foreign private issuer whose results are filed on 6-K): add an entry to `config.yaml` (symbol, English name, `market: US`, `cik`, `metrics`); the `cik` can be found on the company's EDGAR page. The rest is as above: check the parse results the first time, and compare with the full-year figures of the company's 20-F annual report.
- **Add an A-share**: add an entry to `config.yaml` (symbol such as `600036.SS`, English name, `market: CN`, `metrics`); the program downloads periodic and express reports from CNINFO automatically; the Chinese report layout is basically the same across companies (uniform CSRC format); check the parse results the first time.
- **Add a Korean stock**: only Samsung has a dedicated downloader (`providers/samsung.py`) at present; other Korean companies need a downloader for their own IR websites, or a connection to DART (see "To do").
- **Always keep the software English-only** (see [CLAUDE.md](CLAUDE.md)): English stock names, English labels, and no Chinese in anything displayed or generated.

## To do

Low priority, to be done when needed:

- **Apply for an OpenDART API key from DART (the electronic disclosure system of the Korean Financial Supervisory Service)** as a supplementary cross-check source for Korean stocks: DART is the first-hand source of the statutory disclosures of Korean companies (but mostly in Korean, with the English versions incomplete, which is why the company IR websites' English statements are the main source for now). Once connected it could: (1) cross-check the English statements on the IR website with the numbers of the DART quarterly, half-year and annual reports; (2) before the English statements are uploaded, replace the preliminary data of the call material with the exact numbers of the official Korean report (DART quarterly reports are filed within 45 days after the quarter end, basically on the same day as the Korean and English reports on the IR website); (3) be the common way to track other Korean companies later. Applying requires registering with an email address at opendart.fss.or.kr (free); once obtained, provide it as the repository secret `DART_API_KEY` and a local environment variable, never in the code.

- **Cross-check Hikvision's Chinese reports with the English versions**: CNINFO has English periodic reports since 2016, which can be a second source for checking the numbers.

## Keeping it healthy

Tests gate the publication, and a **data freshness check** fails the update job and the scheduled workflow when a price or FX series
stops updating (limits per market in `config.yaml`; the data is still saved and the site still published, and the failure is what
sends the email). The repository also holds the Cloud Monitoring alert policies for a failed or missing update job and the script that
sets them up (`scripts/setup-alerts.sh`). What is in place, what is tested and what is not yet: [docs/operations.md](docs/operations.md).

## Tests

```bash
.venv/bin/pytest
```

Continuous integration (`.github/workflows/ci.yml`): every pull request runs the tests on the oldest and the newest supported Python
(3.10 and 3.12) and builds the container image used on Cloud Run, with a smoke test of the image. The publishing workflow
(`pages.yml`) calls the same tests after every code change and **publishes nothing if they fail**; its scheduled data updates
skip them, because the code has not changed since it last passed. `tests/test_workflows.py` guards that wiring.
