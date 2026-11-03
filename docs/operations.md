# Keeping the platform healthy

What stops a bad change or a silently dead data source from going unnoticed. This page grows with the project; each part says
what is in place and what is not.

## 1. Tests gate the publication (in place)

Every pull request runs the tests on Python 3.10 and 3.12 and builds the container image (`.github/workflows/ci.yml`). After a code
change, the publishing workflow (`pages.yml`) runs the same tests first and publishes nothing if they fail. The scheduled data
updates skip the tests, because the code has not changed since it last passed. `tests/test_workflows.py` guards that wiring.

## 2. Data freshness (in place)

**The failure it catches:** a data source that silently stops delivering. Nothing errors: the program falls back to the saved
data, the page keeps showing numbers, and only the date of the newest day gets older.

**What is checked** (`freshness.py`, run at the end of `valuation-monitor-update` and by `valuation-monitor-check`; no network):

| Series | Limit (calendar days, `config.yaml` `freshness`) | Over the limit |
| --- | --- | --- |
| Newest saved price of each stock | Hong Kong 5, US 5, Korea 6, China 10 | **fails** the job |
| Newest saved FX rate of each pair | 7 | **fails** the job |
| Newest filing of each company | 200 | only a warning |

The price and FX limits are in calendar days because weekends and holidays are not known to the program (trading days are whatever
actually traded). They are long enough for a Chinese Golden Week (8 days off) and for the lag of Yahoo-only markets, whose newest
day can arrive about a day late (see the README). A company that has not published for 200 days is a warning, not a failure:
nothing here can make a company publish. Change the limits in `config.yaml` if a real case shows them too tight or too loose.

**What happens on a failure:**

- *Cloud Run job:* the job saves and uploads the data first, then prints the `STALE` lines and exits with a failure. The failed
  execution is what an alert on the job should react to (step 3 of this page, not set up yet).
- *GitHub workflow:* the data is committed and **the site is still published** (an old page is better than none), and then the
  last step of the deploy job fails the run. A failed scheduled run makes GitHub send an email.

**How to read a line:**

```
OK    0700.HK prices: newest 2026-11-03, 0 days old (limit 5)
STALE 005930.KS prices: newest 2026-10-20, 14 days old (limit 6)
WARN  0700.HK filings: newest 2026-01-15, 299 days old (limit 200)
```

For a `STALE` line: look at the log of the update just above it for `cannot update` or `cannot reach` messages naming the source, open
the source in a browser (Yahoo Finance's chart for that symbol, for example) to see whether it changed or is down, and run
`valuation-monitor-update` locally to see the real error. A source that changed its format needs a fix in `providers/`.

**Known gap:** the check only runs when the job runs. A job that never starts (the schedule disabled, a permission removed) is not
caught by this check; the GitHub side runs independently and would still fail, and an alert on "no successful execution for a long
time" is part of step 3.

## 3. Alerts when a Cloud Run job fails (not set up yet)

Planned: an email from Cloud Monitoring when an execution of `valuation-update` fails, plus one for "no successful execution for
12 hours", tested by making a job fail on purpose before it is relied on.
