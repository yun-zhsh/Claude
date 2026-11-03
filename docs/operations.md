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
  execution is what the alert of section 3 reacts to.
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
caught by this check; the GitHub side runs independently and would still fail, and the "no successful execution for 12 hours"
alert of section 3 covers it.

## 3. Alerts when the Cloud Run job fails (set up on the project and tested; the second alert is not drilled yet)

Two email alerts from Cloud Monitoring, defined in `ops/alerts/` and created by `scripts/setup-alerts.sh`:

| Alert | Fires when | Catches |
| --- | --- | --- |
| `Watchlist monitor: update job failed` | an execution of `valuation-update` ends in failure (within about 5 minutes) | a crash, a version conflict when uploading the database, stale data (section 2) |
| `Watchlist monitor: no successful update for 12 hours` | no execution has succeeded for 12 hours (the schedule is every 4 hours) | the job not starting at all: the schedule disabled, a permission removed; the gap of section 2 |

**Set up** (Cloud Shell, once; the address is given to Cloud Monitoring only, it is not stored in the repository):

```bash
cd ~/Watchlist-monitor && git pull origin main
export ALERT_EMAIL=you@example.com
scripts/setup-alerts.sh
```

The script first checks that the metric the alerts watch (`run.googleapis.com/job/completed_execution_count`, with a `result` label)
exists, so that a wrong name cannot produce an alert that never fires; then it creates the email channel and the two policies. Running it
again changes nothing. Alerting is billed per condition: two policies are cents a month at most (see the Cloud Monitoring pricing page).

**Test it before relying on it** (the first alert; this execution fails before it touches any data, because the configuration file it
is told to use does not exist):

```bash
gcloud run jobs execute valuation-update --region=$REGION --update-env-vars=VALUATION_CONFIG=/nonexistent.yaml --wait
```

An email should arrive within about ten minutes (look in the spam folder the first time); the incident is listed under Cloud Monitoring >
Alerting > Incidents and closes by itself an hour after the last failure. The second alert is not tested this way (it needs 12 hours
without a success); to see it work, pause the schedule for 12 hours (`gcloud scheduler jobs pause valuation-update-every-4h --location=$REGION`),
wait for the email, and resume it (`gcloud scheduler jobs resume ...`).

**Status of the verification:** the policy files are valid Cloud Monitoring alert policies (parsed with Google's own library), the
script's logic is tested against a fake `gcloud` and `curl` (`tests/test_alerts.py`), and it has been run against the real project; the
failed-execution alert has been fired on purpose. The silent-job alert has not been fired yet (it needs 12 hours without a success).

| Date | What was tested | Result |
| --- | --- | --- |
| 2026-11-03 | `scripts/setup-alerts.sh` on the real project | The preflight found `run.googleapis.com/job/completed_execution_count` with a `result` label; the email channel and both policies were created. Listing the policies by display name works, so a second run recognises them. |
| 2026-11-03 | Failed-execution alert: an execution of `valuation-update` made to fail on purpose (`VALUATION_CONFIG=/nonexistent.yaml`, exit code 1, no data touched) | Worked. The incident opened in Cloud Monitoring within minutes (opened 15:37:19 UTC-5, "value of 1.000 above the threshold of 0.000") and the email from Google Cloud Alerting arrived in the inbox with the policy documentation. The email channel reports no verification status, and it was not needed. |
| | Silent-job alert (no successful execution for 12 hours) | not tested yet: pause the schedule for 12 hours as described above |

Notes from the first alert: the email's subject says "No severity", because the policy sets none (a `severity` field could be added to
the policy files; existing policies are not changed by running the script again, so they would have to be deleted and created again).
The incident closes by itself an hour after the last failure.

## To do: checks that have not been done yet

Things that are configured or tested only in part. Tick one off by doing it and writing the result in the table of its section.

| What | Why it is open | How |
| --- | --- | --- |
| Fire the **data freshness failure** in the real GitHub workflow | Only the case "data is fresh" has run there (the failure step was skipped, as it should). That a stale series still publishes the site, then turns the run red and sends the email, is tested in `tests/test_workflows.py` and `tests/test_freshness.py` but has not happened for real. | Commit a throwaway change on `main` that sets every limit in `config.yaml` `freshness.max_price_age_days` to `0` (a stock whose newest day is yesterday is then stale), let the workflow run (or start it by hand), and check three things: the site is still updated, the run ends red at "Fail the run if the data is stale", and GitHub sends the failure email. **Then revert the commit at once**: until then every scheduled run is red. |
| Fire the **silent job** alert (no successful update for 12 hours) | Created, never fired: it needs 12 hours without a success. | `gcloud scheduler jobs pause valuation-update-every-4h --location=$REGION`, wait for the email (about 12 hours), then `gcloud scheduler jobs resume valuation-update-every-4h --location=$REGION`. Do it on a day when nothing depends on fresh data. |
| Measure the **effect of the build cache** of `scripts/deploy-cloud.sh` | The first build had nothing to reuse: 97 s for the build, 126 s in all (2026-11-03). The point of the cache is the next code-only deploy. | Deploy again after a code-only change and write the two figures the script prints ("the build took ...s", "Done in ...s") here, next to the first ones. |
| Add a **severity** to the alert policies | The email subject says "No severity". Cosmetic. | Add `"severity": "ERROR"` to the files in `ops/alerts/`, delete the two policies in Cloud Monitoring and run `scripts/setup-alerts.sh` again (it leaves existing policies as they are). |
