#!/usr/bin/env bash
# Set up the email alerts for the Cloud Run update job: one when an execution fails, one when there has been no successful execution
# for 12 hours (the policies are the files in ops/alerts/). Run it in Cloud Shell, once; running it again changes nothing.
#
#   export ALERT_EMAIL=you@example.com
#   scripts/setup-alerts.sh
#
# The address is not stored in the repository: it is only given to Cloud Monitoring here.
set -euo pipefail

: "${ALERT_EMAIL:?Set ALERT_EMAIL to the address that should receive the alerts, e.g. export ALERT_EMAIL=you@example.com}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT_ID" ] || { echo "No project: set PROJECT_ID or run 'gcloud config set project ...'" >&2; exit 1; }
CHANNEL_NAME="Watchlist monitor email"
METRIC="run.googleapis.com/job/completed_execution_count"
API="https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TOKEN="$(gcloud auth print-access-token)"
call() { curl -sS -H "Authorization: Bearer ${TOKEN}" -H "x-goog-user-project: ${PROJECT_ID}" "$@"; }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }  # read a value from the JSON on stdin

echo "== Cloud Monitoring API"
gcloud services enable monitoring.googleapis.com --project="$PROJECT_ID"

echo "== Is the metric the alerts watch there?"
DESCRIPTOR="$(call "${API}/metricDescriptors/${METRIC}")"
if ! echo "$DESCRIPTOR" | field "'ok' if d.get('type') == '${METRIC}' and any(l.get('key') == 'result' for l in d.get('labels', [])) else ''" | grep -q ok; then
  echo "The metric ${METRIC} (with a 'result' label) was not found, so the alerts would never fire. Answer from the API:" >&2
  echo "$DESCRIPTOR" | head -c 600 >&2
  exit 1
fi
echo "yes: ${METRIC}"

echo "== Email notification channel"
CHANNEL="$(call -G --data-urlencode "filter=displayName=\"${CHANNEL_NAME}\"" "${API}/notificationChannels" | field "(d.get('notificationChannels') or [{}])[0].get('name', '')")"
if [ -z "$CHANNEL" ]; then
  CHANNEL="$(call -X POST -H 'Content-Type: application/json' \
    -d "{\"type\":\"email\",\"displayName\":\"${CHANNEL_NAME}\",\"labels\":{\"email_address\":\"${ALERT_EMAIL}\"}}" \
    "${API}/notificationChannels" | field "d.get('name', '')")"
  [ -n "$CHANNEL" ] || { echo "The notification channel could not be created." >&2; exit 1; }
  echo "created ${CHANNEL}"
else
  echo "exists: ${CHANNEL}"
fi

echo "== Alert policies"
for file in "$HERE"/ops/alerts/*.json; do
  name="$(field "d['displayName']" < "$file")"
  existing="$(gcloud monitoring policies list --project="$PROJECT_ID" --filter="displayName=\"${name}\"" --format='value(name)')"
  if [ -n "$existing" ]; then
    echo "exists, left as it is: ${name}"
  else
    gcloud monitoring policies create --project="$PROJECT_ID" --policy-from-file="$file" --notification-channels="$CHANNEL" >/dev/null
    echo "created: ${name}"
  fi
done

echo
echo "Done. Alerts go to ${ALERT_EMAIL}. To test the first one, make an execution fail on purpose (it changes no data):"
echo "  gcloud run jobs execute valuation-update --region=\${REGION:-asia-east2} --update-env-vars=VALUATION_CONFIG=/nonexistent.yaml --wait"
