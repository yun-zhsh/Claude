# How fast the data grows in git, and when to review it

The scheduled workflow commits the SQLite database (`data/valuation.db`) to git after every update that changed something.
This page records what a simulation predicted about the growth of the repository, what to compare with real numbers and
when, and what to do if the repository gets too big. It is written for whoever develops this next.

## What is in the repository today (measured 2026-11-03, five companies)

| Part | Size |
| --- | --- |
| A fresh clone (git pack) | 74.4 MB |
| of which the original filings under `data/filings/` | 72.2 MB (about 14 MB per company in the pack, 21 MB per company checked out) |
| of which the whole history of `data/valuation.db` | 1.1 MB (2 versions) |
| `data/valuation.db` itself | 4.1 MB (about 0.8 MB per company) |

**The original filings, not the database, are what fills the repository.** They are added once per filing and never change, but
every new company brings its whole history (about 15 MB in the pack for companies whose filings are PDFs).
HTML filings are far cheaper: Meta, Cognizant and ASML (2026-11-04) added 58 MB of HTML files that git packs into **1.3 MB** (the quarterly
releases are similar to each other, so git stores them as differences). The size to watch is therefore the number of PDF companies, not the number of companies. The database is small and grows slowly.

## The simulation

Question: if the database is committed as it is, how big does the repository get with more companies and more time?

Method (`tools/db-growth/`): the five real companies were copied ten times with random noise on every number, so the data
does not compress better than real data would (50 companies, 10 years of history, 4 MB x 10 = about 40 MB of database file).
Then 520 business days were simulated with two commits a day (the Asian markets, then the US one). Each day adds one
price row per symbol and source, and every company files four earnings reports a year. After every checkpoint the repository
was fully repacked and the pack size was measured. The variants:

| Variant | What is committed |
| --- | --- |
| `csvsym` | one CSV per symbol and source: the layout before the database existed |
| `csvtab` | one CSV per table, all symbols in it |
| `sql` | one text dump of the database (rows in the order they were written) |
| `db` | the SQLite file, changed in place; new rows are only inserted (**what the program does**) |
| `dbrw` | the SQLite file, but the derived tables are deleted and rewritten on every run |
| `dbfresh` | the SQLite file rebuilt from scratch on every run |

## Results: size of the repository pack in MB, 50 companies (the original filings are not in this experiment)

| Commits | About | `csvsym` | `csvtab` | `sql` | `db` | `dbrw` | `dbfresh` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | start | 6.6 | 7.0 | 7.3 | 11.3 | 11.3 | 11.3 |
| 261 | 6 months | 9.2 | 7.8 | 15.7 | 25.4 | 38.2 | 47.9 |
| 521 | 1 year | 11.5 | 13.5 | 16.7 | 29.4 | 54.7 | 74.2 |
| 781 | 1.5 years | 13.9 | 14.3 | 17.8 | 34.2 | | |
| 1041 | 2 years | 16.3 | 15.0 | 18.8 | 39.5 | | |

What it says:

- **What the program does (`db`) grows about 18 MB in the first year and about 10 MB in the second**, for 50 companies:
  roughly **0.35 MB per company per year** at first, slowing down. For five companies that is under 2 MB a year.
- **Text is smaller than the binary file** (`sql` and the CSV layouts: about half), but the gap is only some tens of MB at this scale.
- **Rewriting the derived tables on every run multiplies the growth by about 2.4, rebuilding the file by about 3.5.** That is why the code
  writes only rows that changed, never rebuilds or vacuums the file, and has a test that a repeated run leaves the file byte for byte
  identical. Keep it that way.
- The database file itself in the simulation was about 40 MB at the start (matches 10 x the real 4.1 MB) and grew about 3 MB a year.

Limits of the simulation: the numbers are random noise around real data; **retroactive changes were not simulated**, such as Yahoo
re-adjusting a whole price history after a split or distribution (that series is downloaded and written again), a parser version
bump (the parsed filings and derived tables are written again) or a schema change. Each of those is a one-off step up in the repository
size, not a steady growth, and the real numbers will show them. Git's own packing on GitHub may also differ from the local repack.

## 1. When to compare the real growth with the prediction

Compare at these points, and again whenever the number of companies roughly doubles:

| When | Why |
| --- | --- |
| About 4 weeks after the database went live (about 60 data commits) | first sanity check: is the growth anywhere near the prediction, and is every update commit small? |
| After 3 months | enough data commits to see the trend, and at least one quarter's earnings season |
| After 6 months and after 1 year | compare with the 6-month and 1-year rows above |

How to measure (in a fresh clone, so loose objects do not distort it):

```bash
git clone https://github.com/yun-zhsh/Watchlist-monitor.git check && cd check
git gc -q --prune=now
git count-objects -vH                                  # size-pack: what a clone downloads
# size of the database history alone, in MB, and the number of versions:
git rev-list --objects --all -- data/valuation.db \
  | git cat-file --batch-check='%(objecttype) %(objectsize:disk) %(rest)' \
  | awk '$1=="blob" && $3=="data/valuation.db"{n++; s+=$2} END{printf "%d versions, %.1f MB\n", n, s/1e6}'
```

Prediction for the history of `data/valuation.db` alone, append-only: **about 0.35 MB x companies x years in the first year**
(five companies: about 1.8 MB a year, i.e. about 0.15 MB a month), plus the first version. Read the result like this:

- **Within about 50% of the prediction:** the prediction holds; go on to section 2.
- **Much more than predicted (over about twice):** something writes more than it should. Check that a repeated update leaves the
  file unchanged (`sha256sum` before and after running `valuation-monitor-update` twice), look at which commits are large
  (`git log --stat -- data/valuation.db`, then `sqlite3` to compare tables), and look for a one-off cause (a parser version bump,
  a split that made Yahoo rewrite a series).
- **Much less:** fine; update the prediction in this file with the real figure.

Write the measured figures into the table at the top of this file when you do the check, with the date.

## 2. When to check the size again, and when to change the approach

If the real growth matches the prediction, the **database is not the first thing to become a problem; the original filings are**
(about 15 MB in the pack per company, mostly added when a company is first tracked). Check the figures in the table above:

- **Every quarter, and every time several companies are added at once.** The scheduled workflow already fails when
  `data/valuation.db` grows over 50 MB, as a safety net; do not wait for it.
- Thresholds (the pack size of a fresh clone, as measured above; GitHub recommends keeping a repository under 1 GB and says to
  stay well under 5 GB):

| Fresh clone (pack) | Meaning | What to do |
| --- | --- | --- |
| under 200 MB | comfortable | nothing |
| 200-400 MB | getting heavy (a clone takes minutes; in Cloud Shell 70 MB took more than a minute) | plan the change below; do not add many more companies until it is done |
| 400-600 MB | act now | do the change before the next batch of companies |
| over 600 MB | too close to GitHub's 1 GB recommendation | stop adding companies until it is done |
| `data/valuation.db` over 25 MB | half-way to the workflow's tripwire | look at section 1; if the growth is as predicted, plan the change |

At about 14 MB of filings per company, **the 200 MB line is reached at roughly 12 to 15 companies and the 600 MB line at about 40**,
unless the filings move out of git first. The database alone (about 0.35 MB per company per year) would take decades.

### What to change, in the order I would do it

1. **Take the original filings out of git** (the big win). Keep them in the Cloud Storage bucket (they are already there for the
   cloud deployment) or only keep their link and a checksum in the database and download them from the source when a parser
   change needs a re-parse. The parsed values stay in the database, so day-to-day updates do not need the originals. The
   `filings` index would move into the database at the same time. This alone removes about 95% of today's repository.
2. **Stop committing the database, if the GitHub Pages site is no longer needed.** Run only locally and on Google Cloud (the bucket
   keeps old versions for 30 days, which can be extended). If Pages stays, let the workflow download the database from the bucket
   (needs a one-time Workload Identity setup between GitHub and Google Cloud), or commit a sorted text dump of the tables, which
   in the simulation took about half the space of the binary file and shows readable changes.
3. **Put the data in a separate repository** (for example `watchlist-data`), so the code repository stays small and clones fast.
   Old history in the code repository stays as it is; it is cheap to leave.
4. **Squash the data history now and then** (replace the history of the data branch by one commit) if the history of the
   data itself is not valued. This loses the old versions, so it is the last resort.
5. **At hundreds of companies or gigabytes of data**, SQLite itself is still fine (it handles far more); what would need a
   second look is how the page loads data and whether per-company databases are better. A database server (Postgres, Cloud SQL)
   is not warranted before that: it costs money every month and adds a service to run.

## Re-running the simulation

```bash
python -m valuation_monitor.legacy export-csv /tmp/real     # the real data in the CSV layout
for v in db sql dbrw dbfresh csvtab csvsym; do
  python tools/db-growth/sim.py $v /tmp/real /tmp/run 10 520 &   # 10 copies = 50 companies, 520 business days
done; wait
tools/db-growth/measure.sh /tmp/run db 1 261 521 781 1041        # the pack size after N commits; one variant per call
```

Run the variants in parallel on a machine with a few GB of memory; it takes about half an hour. The result of one commit
count differs a little from run to run (git picks different deltas), so read trends, not single digits.
