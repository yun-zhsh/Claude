# Asking an AI assistant: the MCP server

`valuation-monitor-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server. It lets an AI assistant (Claude Desktop,
Claude Code, any MCP client) answer questions such as *"Where does Samsung's PB stand over the last ten years?"* from the data this
project saved, **without letting the assistant make anything up**. Four rules decide the design; each is enforced in code and covered by
tests (`tests/test_mcp_server.py`), not just requested in a prompt.

| Rule | What it means | Where it is enforced |
| --- | --- | --- |
| **Read-only** | A tool call cannot change or download anything. | The database is opened with SQLite's `mode=ro` and `PRAGMA query_only` (`db.py`: `Database(readonly=True)`); the server refuses to start on a writable database; the network layer is switched off (`Monitor(offline=True)`) and the request cache directory is `/dev/null`. Tests: writes raise, no file changes (hashes), sockets are blocked and nothing connects. |
| **Sourced** | Every number comes with where it came from. | Each result lists the earnings record used (period, **publication time** with its time zone, **effective trading day**, **link to the original filing**), every figure that went into the per-share value (period, value, unit, first publication, link, number of confirming filings), the price used (day, source, cross-check status) and the **calculation written out by `audit.py`**, the same text as the chart tooltips on the web page. |
| **Point in time** | Asking about a past day gives the answer someone would have got on that day. | Every tool takes `as_of` (YYYY-MM-DD). Only prices up to that day and filings already in effect by then are used, and the percentile window ends there. The strongest test removes every filing published after the day and checks that the answer is identical. |
| **No model arithmetic** | The assistant does not calculate. | Ratios, percentiles, levels and statistics are computed in Python by the same code as the web page (`metrics/`, `analysis.py`); a test compares the two. The server returns finished numbers and tells the assistant to quote them as given. |

## Running it in the cloud

Besides stdio on your own machine, the same server runs as a web service on Google Cloud Run (`--transport streamable-http`), so an assistant
anywhere can use it by its address, without installing anything. Setup is one command in Cloud Shell (after the other parts of
[docs/deploy-google-cloud.md](deploy-google-cloud.md) are in place):

```bash
cd ~/Watchlist-monitor && git pull origin main && scripts/deploy-cloud.sh mcp
```

It prints the address, `https://valuation-mcp-....run.app/mcp`. Clients that take a URL use it as is, for example
`claude mcp add --transport http valuation-monitor https://valuation-mcp-....run.app/mcp`; custom connectors in other assistants
(ChatGPT, Claude.ai, Gemini) ask for the same address, and whether your plan offers them is up to the provider. A quick check without any
assistant:

```bash
curl -s -X POST https://valuation-mcp-....run.app/mcp -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**The four rules hold in the cloud as well, and the open endpoint adds guards:**

| Guard | How |
| --- | --- |
| Cannot write | It runs as its own service account (`valuation-mcp`) whose only role is `roles/storage.objectViewer` on the data bucket, with the bucket mounted read-only. The update job's account, the SEC secret and the permission to start the job are not given to it. The database is opened read-only as before. |
| Limited traffic | At most 60 requests a minute from one address and 600 in all (per instance, `VALUATION_MCP_PER_CLIENT_LIMIT` / `VALUATION_MCP_TOTAL_LIMIT`), at most 2 instances, 20 concurrent requests each, 60 s per request, 64 KB per request, plus the spend cap and budget alerts of the project. Over the limit the answer is HTTP 429. |
| Nothing held open | Stateless requests, POST only: there are no sessions and no streams a caller could keep open. |
| Logged without addresses | One line per request in Cloud Logging with a short hash of the address. |
| Fresh data | It downloads the database object when a newer version exists, checked every ten minutes, so the update job's results appear without a redeploy. |

**Switching it off** takes one command and keeps the service for later:

```bash
gcloud run services remove-iam-policy-binding valuation-mcp --region=$REGION --member=allUsers --role=roles/run.invoker
```

(`gcloud run services delete valuation-mcp --region=$REGION` removes it.) It costs next to nothing when idle: it scales to zero.

## Tools

| Tool | Use |
| --- | --- |
| `list_stocks` | Symbols, metrics, how far the saved data reaches and how old the newest price is, the valuation levels. |
| `get_valuation(symbol, metric, years, as_of)` | The value, its percentile and level, the statistics of the window, sources and calculation, and **warnings**. |
| `get_history(symbol, metric, years, as_of, points)` | Values over the window, thinned to at most 240 dates, with the minimum and maximum **and the days they happened** (thinning can skip a peak). |
| `get_filings(symbol, as_of, limit)` | The earnings filings in effect on that day, newest first, with publication time, effective trading day and link. |

All four are declared `readOnlyHint`, non-destructive and closed-world in the protocol, so a client can show them as safe to auto-approve.

**Warnings are part of the answer.** They say when the history is shorter than the years asked for (the percentile is then within a
shorter window), when the newest earnings are preliminary (an earnings call deck or express report, replaced when the official report
is out), when the newest price is old, and when `as_of` is after the saved data. A request that cannot be answered (unknown symbol,
a metric the company does not have, a day before the first filing, a loss period) is an error that says why; it never returns a guess.

## Setting it up

Install with the `mcp` extra and point the server at the data (the repository's `data/` folder, or any copy of `valuation.db`):

```bash
pip install -e '.[mcp]'
valuation-monitor-mcp --data-dir data      # speaks MCP over stdio; normally started by the client
```

Claude Code:

```bash
claude mcp add valuation-monitor -- valuation-monitor-mcp --data-dir /path/to/Watchlist-monitor/data
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "valuation-monitor": {
      "command": "valuation-monitor-mcp",
      "args": ["--data-dir", "/path/to/Watchlist-monitor/data", "-c", "/path/to/Watchlist-monitor/config.yaml"]
    }
  }
}
```

`git pull` brings the newest `data/valuation.db` committed by the scheduled workflow; the server notices the changed file and reloads it.

## An example, from the saved data of 2026-11-03

The question: *"Where does Samsung's PB stand over the last ten years?"* The assistant calls
`get_valuation(symbol="005930.KS", metric="pb", years=10)`. The result (shortened; everything below is real output):

```json
{
  "as_of": "2026-11-02", "value": 2.5082, "percentile": 92.6, "level": "Deeply overvalued",
  "window": {"years_asked": 10, "start": "2016-11-02", "end": "2026-11-02", "trading_days": 2429, "covers_years_asked": true},
  "history_statistics": {"mean": 1.614, "min": 0.8844, "max": 5.0572, "at_percentile": {"10": 1.1634, "50": 1.4528, "90": 2.1114}},
  "calculation": {
    "ratio": "PB = close / (per-share value x FX rate) = 229,000 KRW / (91,301.3691 KRW x 1) = 2.5082",
    "per_share_value": ["Book value per share = equity at period end 599,010,902 / shares 6,560.81 = 91,301.3691", "..."]
  },
  "sources": {
    "earnings_record": {"period": "2026Q3", "published": "2026-10-29 10:00", "published_timezone": "Asia/Seoul",
                        "effective_trading_day": "2026-10-29", "preliminary": true,
                        "filing_url": "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2026_3Q_conference_eng.pdf"},
    "price": {"day": "2026-11-02", "close": 229000.0, "currency": "KRW", "source": "yahoo", "cross_check": "yahoo_only"}
  },
  "warnings": ["The latest earnings used are preliminary (earnings call presentation or express report); they are replaced when the official report is out"]
}
```

An answer built only from this result reads like: *Samsung's PB is 2.51 as of 2 November 2026, higher than on 92.6% of the trading days in
the last ten years (range 0.88 to 5.06, median 1.45), which the project's levels call "Deeply overvalued". It is computed as the
229,000 KRW close divided by a book value per share of 91,301 KRW, from the 2026 Q3 figures published on 29 October 2026
([source](https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2026_3Q_conference_eng.pdf)). Note that those figures are preliminary (the
earnings call deck, not yet the audited statements).* (The wording is an example of what an assistant can write from the result; the
numbers and the link are the server's.)

The same question about the past, `as_of="2025-03-31"`, gives PB 1.00 at the 4.1th percentile ("Deeply undervalued"), computed from the 2024 Q4
statements published on 31 January 2025, with a warning that only 9.4 years of history existed by then. Nothing published after
that day is used.

## What the rules do not cover

Being honest about the limits is part of the design.

- **The assistant can still misquote.** The server returns finished, sourced numbers and instructions to quote them as given, and the tool
  descriptions ask for the sources and warnings to be repeated; it cannot force a model to do so. The result is built so a reader can check
  every claim against the filing link.
- **Point in time covers data, not corrections.** Filings and prices are used as they were published. One thing is applied with today's
  knowledge, and it is shown in the calculation text: per-share figures and prices before a stock split are converted to the current share
  basis. A restated figure is not used: the first published value stays, and disagreements between filings are flagged in the reports.
- **Yahoo-only markets can lag.** US, Korean and A-share prices come from one source, whose newest day can arrive about a day late
  (see Known limitations in the README). `list_stocks` and the warnings show how old the newest price is.
- **Not investment advice.** A percentile of a company's own past says nothing about its future or about other companies.
- **Local by default, public when you deploy it.** Run on your machine (stdio) the server has no port and no credentials. The Cloud Run
  service below is open to anyone who has the address, deliberately: the data is public (original filings and prices, the same as the
  GitHub Pages site) and the service can only read it, so what an open endpoint risks is abuse and cost, not leaks. Those are limited
  (see below), not eliminated. If you would rather not have it public, do not run `scripts/deploy-cloud.sh mcp`, or switch it off at once
  (see "Switching it off").
- **Text going to the model is produced by this code.** Results contain numbers, dates, links and fixed sentences; no text copied from a
  filing or a web page is passed through, which keeps prompt injection through the data out of the picture.
