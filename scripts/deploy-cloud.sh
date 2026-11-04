#!/usr/bin/env bash
# Build the container image ONCE and deploy the update job and the web page from it.
#
#   scripts/deploy-cloud.sh          # build, then deploy the job and the web page
#   scripts/deploy-cloud.sh job      # build, then deploy only the update job
#   scripts/deploy-cloud.sh web      # build, then deploy only the web page
#   scripts/deploy-cloud.sh mcp      # build, then deploy only the PUBLIC read-only MCP service (never part of "all"; see docs/mcp-server.md)
#
# Run it in Cloud Shell from an up-to-date clone (git pull). Settings come from the environment, with defaults that match
# docs/deploy-google-cloud.md: PROJECT_ID (the active gcloud project), REGION (asia-east2), BUCKET (PROJECT_ID-data),
# SA (valuation-runner@PROJECT_ID.iam.gserviceaccount.com). It is safe to run again; nothing here changes the data.
set -euo pipefail

WHAT="${1:-all}"
case "$WHAT" in all | job | web | mcp) ;; *) echo "Usage: $0 [all|job|web|mcp]" >&2; exit 2 ;; esac

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT_ID" ] || { echo "No project: set PROJECT_ID or run 'gcloud config set project ...'" >&2; exit 1; }
REGION="${REGION:-asia-east2}"
BUCKET="${BUCKET:-${PROJECT_ID}-data}"
SA="${SA:-valuation-runner@${PROJECT_ID}.iam.gserviceaccount.com}"
MCP_ACCOUNT="${MCP_ACCOUNT:-valuation-mcp}"
MCP_SA="${MCP_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO="${REPO:-cloud-run-source-deploy}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/valuation-monitor"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then TAG="${TAG}-modified-$(date +%H%M%S)"; fi
JOB="projects/${PROJECT_ID}/locations/${REGION}/jobs/valuation-update"
DB="gs://${BUCKET}/valuation.db"

echo "Project ${PROJECT_ID}, region ${REGION}, bucket ${BUCKET}, image ${IMAGE}:${TAG}"
SECONDS=0
phase() { echo; echo "== $1 (elapsed so far: ${SECONDS}s)"; }

phase "Image repository"
gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION"

phase "Build the image (reuses the layers of the last build)"
BUILD_START=$SECONDS
gcloud builds submit --config cloudbuild.yaml --region="$REGION" --substitutions="_IMAGE=${IMAGE},_TAG=${TAG}" .
BUILD_SECONDS=$((SECONDS - BUILD_START))

if [ "$WHAT" = all ] || [ "$WHAT" = job ]; then
  phase "Deploy the update job"
  gcloud run jobs deploy valuation-update --image "${IMAGE}:${TAG}" --region "$REGION" \
    --service-account "$SA" \
    --command valuation-monitor-update \
    --set-secrets SEC_USER_AGENT=sec-user-agent:latest \
    --set-env-vars "VALUATION_DB_URI=${DB}" \
    --add-volume "name=data,type=cloud-storage,bucket=${BUCKET}" \
    --add-volume-mount volume=data,mount-path=/data \
    --task-timeout 30m --max-retries 1
fi

if [ "$WHAT" = all ] || [ "$WHAT" = web ]; then
  phase "Deploy the web page"
  gcloud run deploy valuation-web --image "${IMAGE}:${TAG}" --region "$REGION" \
    --service-account "$SA" \
    --set-secrets SEC_USER_AGENT=sec-user-agent:latest \
    --set-env-vars "VALUATION_DB_URI=${DB},VALUATION_UPDATE_JOB=${JOB}" \
    --add-volume "name=data,type=cloud-storage,bucket=${BUCKET}" \
    --add-volume-mount volume=data,mount-path=/data \
    --iap --no-allow-unauthenticated \
    --min-instances 0 --max-instances 1 --memory 1Gi --timeout 900
fi

if [ "$WHAT" = mcp ]; then
  # The service is open to everyone, so it runs as an account that can do nothing but read the bucket: even a flaw in the code could not write the data.
  phase "Service account that can only read the bucket"
  gcloud iam service-accounts describe "$MCP_SA" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$MCP_ACCOUNT" --display-name="Valuation MCP service (read-only)"
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="serviceAccount:${MCP_SA}" --role=roles/storage.objectViewer >/dev/null

  phase "Deploy the MCP service (public, read-only)"
  gcloud run deploy valuation-mcp --image "${IMAGE}:${TAG}" --region "$REGION" \
    --service-account "$MCP_SA" \
    --command valuation-monitor-mcp \
    --args=--transport,streamable-http,--host,0.0.0.0 \
    --set-env-vars "VALUATION_DB_URI=${DB}" \
    --add-volume "name=data,type=cloud-storage,bucket=${BUCKET},readonly=true" \
    --add-volume-mount volume=data,mount-path=/data \
    --allow-unauthenticated \
    --min-instances 0 --max-instances 2 --concurrency 20 --memory 1Gi --timeout 60
  MCP_URL="$(gcloud run services describe valuation-mcp --region "$REGION" --format='value(status.url)')/mcp"
  echo "MCP endpoint: ${MCP_URL}"

  # The service is only as good as every stock it can load under its own conditions (no secret, read-only bucket): ask it for all of them.
  phase "Check the deployed service (every stock must load)"
  REPLY="$(mktemp)"
  curl -sS -m 120 -X POST "$MCP_URL" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -o "$REPLY" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_stocks","arguments":{}}}' \
    && python3 - "$REPLY" <<'PY' \
    || { echo "The MCP service is deployed but NOT healthy: see above. To switch it off: gcloud run services remove-iam-policy-binding valuation-mcp --region=$REGION --member=allUsers --role=roles/run.invoker" >&2; exit 1; }
import json, sys

reply = json.load(open(sys.argv[1]))
if "result" not in reply or reply["result"].get("isError"):
    sys.exit(f"ERROR: list_stocks failed: {reply}")
stocks = json.loads(reply["result"]["content"][0]["text"])["stocks"]
failed = [f"{s['symbol']}: {s['error']}" for s in stocks if "error" in s]
print(f"{len(stocks) - len(failed)} of {len(stocks)} stocks load")
if failed:
    sys.exit("ERROR: the deployed service cannot load " + "; ".join(failed))
PY
fi

echo
echo "Done in ${SECONDS}s (the build took ${BUILD_SECONDS}s). Image ${IMAGE}:${TAG}"
