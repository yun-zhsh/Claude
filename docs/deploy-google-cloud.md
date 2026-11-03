# Running the web page on Google Cloud

> **Already deployed with the earlier CSV version?** See "Upgrading an existing deployment" at the end: the data moved into a
> SQLite database (`data/valuation.db`), which is kept in a bucket object instead of a mounted folder.

This runs the dynamic page (the one with the "Update data" button) on Google Cloud, visible only to your Google account.
The GitHub Pages site and its 4-hourly workflow are untouched and keep working side by side.

## How it fits together

```
Cloud Scheduler (every 4 hours)
        |  starts
        v
Cloud Run job  "valuation-update"   pulls the database, fetches prices and filings, pushes the database back
        |  downloads / uploads                        ^ starts it ("Update data" button)
        v                                             |
Cloud Storage bucket:  valuation.db (object)  ---->  Cloud Run service "valuation-web"  (the web page, reads a downloaded copy)
                       filings/ (mounted at /data)                  ^
                                                       Identity-Aware Proxy: only your Google account
```

- **One image, two uses.** The same container image serves the page (default command) and runs the update job
  (command `valuation-monitor-update`).
- **The data is not in the image.** The database lives in the bucket as the object `valuation.db`. A SQLite file cannot sit on a
  mounted folder (it needs file locking), so the job and the page each work on a copy on their own disk (`cloud.py`):
  the job downloads it, updates it and uploads it again only if it changed, and only if nobody else uploaded a newer version
  in the meantime (so two jobs at once cannot overwrite each other: the second one fails and nothing is lost). The page
  downloads a newer version when there is one, checked every 10 minutes (`VALUATION_RELOAD_SECONDS`). The original filings
  are plain files in the same bucket, mounted at `/data`.
- **The "Update data" button** starts the update job (it does not fetch by itself) and the page says so; the new data appears
  after about a minute plus the page's own 10-minute check. The page never uploads anything.
- **Login** is handled by Identity-Aware Proxy (IAP): visitors sign in with a Google account, and only accounts you
  list can get in.
- **Secrets**: `SEC_USER_AGENT` (your contact for SEC EDGAR) is kept in Secret Manager, never in the repository.

Expected cost: a few cents to about one dollar a month (Cloud Run scales to zero when nobody is using it; the bucket
holds about 100 MB). Set a budget alert (step 1) so you are told if that ever changes.

## Before you start

You need a Google account with billing enabled. Everything below is run in **Cloud Shell**
(the `>_` button at the top right of https://console.cloud.google.com), which already has `gcloud` and `git`.
Replace the values in the first block once, and keep the same Cloud Shell tab open.

```bash
export PROJECT_ID=watchlist-monitor-12345   # must be globally unique: change the digits
export REGION=asia-east2                    # Hong Kong; any Cloud Run region works
export BUCKET=${PROJECT_ID}-data
export ME=your-google-account@gmail.com     # the account that may open the page
export SEC_UA="valuation-monitor your-contact-email"   # the identity SEC EDGAR asks for
```

## 1. Create a project and a budget alert

```bash
gcloud projects create $PROJECT_ID --name="Watchlist monitor"
gcloud config set project $PROJECT_ID
gcloud billing accounts list
```

Link billing (use the ACCOUNT_ID from the list above):

```bash
gcloud billing projects link $PROJECT_ID --billing-account=ACCOUNT_ID
```

Set up cost protection in the console (Billing -> Budgets & caps -> Create new). Two budgets are worth having:

1. **A spend cap on Cloud Run** (the service that can scale): in the wizard choose the spend cap type, project `Watchlist monitor`,
   service **Cloud Run**, amount 5 USD. The Budgets page says services are paused when a cap is exceeded, and an enforced cap
   resets at the start of each month. Costs are recorded within about 24 hours, so a cap can be exceeded slightly.
   A spend cap can only cover one service; the wizard has no "all services" option.
2. **An alert budget for the whole billing account**: type *budget alert*, all services, 5 USD, thresholds 50/90/100%,
   email notifications on. It only sends email.

Enable the services used:

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com iap.googleapis.com storage.googleapis.com
```

## 2. Create the bucket and load the current data

The bucket keeps old versions for 30 days (a safety net if bad data is ever written); versioning can be removed later.

```bash
gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access
gcloud storage buckets update gs://$BUCKET --versioning
cat > /tmp/lifecycle.json <<'JSON'
{"rule": [{"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 30, "isLive": false}}]}
JSON
gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lifecycle.json
```

Copy the repository's `data/` folder into the bucket: this puts the database at `gs://$BUCKET/valuation.db` and the original
filings under `filings/` (this is the starting point; after that the job keeps the database up to date):

```bash
git clone https://github.com/yun-zhsh/Watchlist-monitor.git
cd Watchlist-monitor
gcloud storage rsync data gs://$BUCKET --recursive
```

## 3. Store the SEC identity as a secret

```bash
printf '%s' "$SEC_UA" | gcloud secrets create sec-user-agent --data-file=-
```

## 4. Service account for the job and the page

```bash
gcloud iam service-accounts create valuation-runner --display-name="Watchlist monitor runner"
export SA=valuation-runner@${PROJECT_ID}.iam.gserviceaccount.com
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$SA --role=roles/storage.objectUser
gcloud secrets add-iam-policy-binding sec-user-agent --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
```

## 5. Build the image and deploy the update job

The first build in a new project may fail with a permission error about the Cloud Build service account; if so, run
this once and repeat the command (replace NUMBER with the project number from `gcloud projects describe $PROJECT_ID --format='value(projectNumber)'`):

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member=serviceAccount:NUMBER-compute@developer.gserviceaccount.com --role=roles/cloudbuild.builds.builder
```

Run these from the cloned `Watchlist-monitor` folder:

```bash
gcloud run jobs deploy valuation-update \
  --source . --region=$REGION \
  --service-account=$SA \
  --command=valuation-monitor-update \
  --set-secrets=SEC_USER_AGENT=sec-user-agent:latest \
  --set-env-vars=VALUATION_DB_URI=gs://$BUCKET/valuation.db \
  --add-volume=name=data,type=cloud-storage,bucket=$BUCKET \
  --add-volume-mount=volume=data,mount-path=/data \
  --task-timeout=30m --max-retries=1
```

Run it once by hand to check it works. Do not use `--wait`: it keeps the terminal busy, and pressing Ctrl+C only stops
waiting (the job keeps running in the cloud).

```bash
gcloud run jobs execute valuation-update --region=$REGION
```

After a minute or two read the log:

```bash
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="valuation-update" AND textPayload:("Data directory" OR "updated" OR "ERROR" OR "cannot")' --limit=30 --format="value(timestamp,textPayload)" --freshness=1h --order=asc
```

The first lines must be `Data directory: /data` and `Database: gs://.../valuation.db ...: downloaded`, then one "updated" line per stock,
then `Database uploaded` (or `Database unchanged` when nothing new came in). If you see
dozens of "new results release" lines instead, the job is not using the saved data. If one stock fails, the message says why
(a network problem, a changed filing layout, ...). A normal run takes about a minute.

## 6. Deploy the web page, visible only to you

The page's "Update data" button starts the update job, so its service account (the same one) needs the Run Invoker role on the job:

```bash
gcloud run jobs add-iam-policy-binding valuation-update --region=$REGION \
  --member=serviceAccount:$SA --role=roles/run.invoker
```

```bash
gcloud run deploy valuation-web \
  --source . --region=$REGION \
  --service-account=$SA \
  --set-secrets=SEC_USER_AGENT=sec-user-agent:latest \
  --set-env-vars=VALUATION_DB_URI=gs://$BUCKET/valuation.db,VALUATION_UPDATE_JOB=projects/$PROJECT_ID/locations/$REGION/jobs/valuation-update \
  --add-volume=name=data,type=cloud-storage,bucket=$BUCKET \
  --add-volume-mount=volume=data,mount-path=/data \
  --iap --no-allow-unauthenticated \
  --min-instances=0 --max-instances=1 --memory=1Gi --timeout=900
```

`--max-instances=1` matters: it keeps a single page server, so two instances never write to the bucket at the same time.

Allow your own Google account through IAP:

```bash
gcloud iap web add-iam-policy-binding \
  --resource-type=cloud-run --service=valuation-web --region=$REGION \
  --member=user:$ME --role=roles/iap.httpsResourceAccessor
```

(If your `gcloud` does not know this command, open the console: Security -> Identity-Aware Proxy -> find `valuation-web`
-> Add principal -> your account -> role "IAP-secured Web App User".)

### 6b. Only if you have no Google Workspace organization (a personal Google account)

With a personal account the project is not inside an organization, so IAP has no OAuth client and the page shows
`Empty Google Account OAuth client ID(s)/secret(s).` Create one in the console (these steps are not available in `gcloud`):

1. Open https://console.cloud.google.com/auth/overview (check the project in the top bar) -> **Get started**: app name
   `Watchlist monitor`, your email as support and contact email, audience **External**. Then Audience -> **Test users** ->
   add your own account and keep the status **Testing**. Only test users can sign in.
2. Clients -> **Create client** -> application type **Web application**, name `IAP Cloud Run`, create it.
   Copy the **Client ID** and the **Client secret**. The secret is a password: keep it out of chats and the repository.
3. Edit the client and add this under **Authorized redirect URIs**, with your own client ID, then save
   (it can take a few minutes to apply):
   `https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect`
4. Open https://console.cloud.google.com/security/iap, find `valuation-web` (Cloud Run), open its settings and enter the
   Client ID and Client secret as the custom OAuth client. Save.

The first sign-in shows "Google hasn't verified this app" because the consent screen is in Testing: click **Advanced**, then
**Go to Watchlist monitor (unsafe)**, and allow.

Get the address and open it in the browser. Google asks you to sign in; the first request after a quiet period takes
a few seconds (the instance starts from zero):

```bash
gcloud run services describe valuation-web --region=$REGION --format='value(status.url)'
```

## 7. Schedule the update every 4 hours

Cloud Scheduler starts the job through the Cloud Run API:

```bash
gcloud run jobs add-iam-policy-binding valuation-update --region=$REGION \
  --member=serviceAccount:$SA --role=roles/run.invoker
gcloud scheduler jobs create http valuation-update-every-4h \
  --location=$REGION --schedule="47 1-23/4 * * *" --time-zone="Etc/UTC" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/valuation-update:run" \
  --http-method=POST --oauth-service-account-email=$SA
```

This runs at 01:47, 05:47, ... UTC like the GitHub workflow. To test it right away:

```bash
gcloud scheduler jobs run valuation-update-every-4h --location=$REGION
```

## Updating later

After code or `config.yaml` changes are merged, in Cloud Shell:

```bash
cd ~/Watchlist-monitor && git pull origin main
scripts/deploy-cloud.sh          # or: scripts/deploy-cloud.sh job   /   scripts/deploy-cloud.sh web
```

The script builds the container image **once** and deploys the update job and the web page from it (the commands of steps 5 and 6 build
twice, one image each). It also makes rebuilds faster in two ways: `.gcloudignore` keeps `data/` (the original filings, about 70 MB) out
of the upload, and `cloudbuild.yaml` reuses the layers of the previous build while the `Dockerfile` installs the dependencies in a
layer of their own, so a code-only change does not reinstall pandas and the rest. The first run after this change builds everything
once, as before; later code-only builds should be clearly shorter. The script prints how long the build and the whole run took,
so you can see the effect. The settings default to the values of this guide (`PROJECT_ID`, `REGION`, `BUCKET`, `SA` from the
environment override them). The data in the bucket is not touched.

Small things need no deployment at all: parsing, calculation, pages and downloads can be developed and tested locally with the
saved data (`pytest`, `valuation-monitor-web`), and only changes to what is specific to the cloud (`cloud.py`, `wsgi.py`, the
`Dockerfile`) need a real deployment to try.

## Upgrading an existing deployment (from the CSV version)

If the job and page were deployed before the database existed, they keep running on the CSV files in the bucket. To switch
(variables from "Before you start" must be set; `$SA` is the service account of step 4):

1. In the clone: `git pull origin main`.
2. Put the database into the bucket: `gcloud storage cp data/valuation.db gs://$BUCKET/valuation.db`
   (the job catches up with the days that are missing on its next run).
3. Redeploy the job and the page with `scripts/deploy-cloud.sh` (or the commands of steps 5 and 6) (both now carry `VALUATION_DB_URI`;
   the page also `VALUATION_UPDATE_JOB`), and give the service account the Run Invoker role on the job (first command of step 6).
4. Run the job once (step 5) and check the log for `Database: ... downloaded` and `Database uploaded`, then open the page.
5. Optional clean-up of the old CSV files in the bucket: `gcloud storage rm -r gs://$BUCKET/prices gs://$BUCKET/fx gs://$BUCKET/eps`
   (the bucket keeps old versions for 30 days).

## Troubleshooting

- **`Empty Google Account OAuth client ID(s)/secret(s).`:** see step 6b.
- **The page shows a permission error:** your account is not on the IAP list (step 6), or you are signed in with a
  different Google account.
- **The page is empty or an old date shows:** check that the job ran (`gcloud run jobs executions list --job=valuation-update --region=$REGION`)
  and wait up to 10 minutes for the page to reload, or press "Update data".
- **"The update could not be started" after pressing "Update data":** the service account lacks the Run Invoker role on the job
  (step 6), or `VALUATION_UPDATE_JOB` is wrong; the service log has the exact error (Cloud Run -> `valuation-web` -> Logs).
- **The job fails with "was changed by someone else since it was downloaded":** two executions overlapped (for example the schedule
  and a manual run); the one that finished first kept its data. Run the job again.
- **The page keeps an old version of the data:** it downloads a newer database at most every 10 minutes; check that the job
  uploaded one (`gcloud storage ls -l gs://$BUCKET/valuation.db`).
- **SEC messages about `SEC_USER_AGENT`:** the secret in step 3 is missing or the service account cannot read it.
- **Remove everything:** `gcloud projects delete $PROJECT_ID` removes the project and all its resources.
