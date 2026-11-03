"""The alert policies and the script that sets them up. Nothing here talks to Google: the script runs against fake `gcloud` and `curl`."""

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POLICIES = sorted((ROOT / "ops" / "alerts").glob("*.json"))
SCRIPT = ROOT / "scripts" / "setup-alerts.sh"


def test_policies_watch_the_update_job_and_name_no_channel_or_address():
    assert {p.name for p in POLICIES} == {"job-failed.json", "job-silent.json"}
    for path in POLICIES:
        text = path.read_text(encoding="utf-8")
        policy = json.loads(text)
        assert "notificationChannels" not in policy and "@" not in text  # the script adds the channel; no address is stored here
        (condition,) = policy["conditions"]
        spec = condition.get("conditionThreshold") or condition["conditionAbsent"]
        assert "run.googleapis.com/job/completed_execution_count" in spec["filter"] and 'job_name="valuation-update"' in spec["filter"]
    failed = json.loads((ROOT / "ops/alerts/job-failed.json").read_text(encoding="utf-8"))["conditions"][0]["conditionThreshold"]
    assert 'metric.label.result="failed"' in failed["filter"] and failed["thresholdValue"] == 0
    silent = json.loads((ROOT / "ops/alerts/job-silent.json").read_text(encoding="utf-8"))["conditions"][0]["conditionAbsent"]
    assert 'metric.label.result="succeeded"' in silent["filter"] and silent["duration"] == "43200s"  # 12 hours: three missed 4-hourly runs


def test_the_job_name_in_the_policies_is_the_one_the_deploy_script_creates():
    assert "valuation-update" in (ROOT / "scripts" / "deploy-cloud.sh").read_text(encoding="utf-8")


FAKE_GCLOUD = '''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
open(os.environ["CALLS"], "a").write("gcloud " + " ".join(args) + "\\n")
if args[:3] == ["config", "get-value", "project"]:
    print("test-project")
elif args[:2] == ["auth", "print-access-token"]:
    print("token")
elif args[:3] == ["monitoring", "policies", "list"]:
    name = next(a for a in args if a.startswith("--filter=")).split('"')[1]
    if name in os.environ.get("EXISTING_POLICIES", "").split("|"):
        print("projects/test-project/alertPolicies/1")
'''

FAKE_CURL = '''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
open(os.environ["CALLS"], "a").write("curl " + " ".join(args) + "\\n")
url = args[-1]
if "metricDescriptors" in url:
    print(os.environ["DESCRIPTOR"])
elif "-X" in args:  # creating the channel
    print('{"name": "projects/test-project/notificationChannels/42"}')
elif "notificationChannels" in url:
    print(os.environ.get("CHANNELS", "{}"))
'''

GOOD_DESCRIPTOR = '{"type": "run.googleapis.com/job/completed_execution_count", "labels": [{"key": "result"}, {"key": "job_name"}]}'


def run_script(tmp_path, descriptor=GOOD_DESCRIPTOR, channels="{}", existing=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls.txt"
    calls.write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls), "DESCRIPTOR": descriptor,
           "CHANNELS": channels, "EXISTING_POLICIES": existing, "ALERT_EMAIL": "me@example.com"}
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    return result, calls.read_text()


needs_tools = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None, reason="bash and python3 are needed")


def test_script_is_valid_bash_and_asks_for_the_address():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    env = {k: v for k, v in os.environ.items() if k != "ALERT_EMAIL"}
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert result.returncode != 0 and "ALERT_EMAIL" in result.stderr


@needs_tools
def test_first_run_creates_the_channel_and_both_policies(tmp_path):
    result, calls = run_script(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "created projects/test-project/notificationChannels/42" in result.stdout
    assert '"email_address":"me@example.com"' in calls  # the address goes to the API, not into a file
    created = [line for line in calls.splitlines() if line.startswith("gcloud monitoring policies create")]
    assert len(created) == 2 and all("--notification-channels=projects/test-project/notificationChannels/42" in line for line in created)
    assert "created: Watchlist monitor: update job failed" in result.stdout


@needs_tools
def test_second_run_changes_nothing(tmp_path):
    channels = '{"notificationChannels": [{"name": "projects/test-project/notificationChannels/42"}]}'
    both = "Watchlist monitor: update job failed|Watchlist monitor: no successful update for 12 hours"
    result, calls = run_script(tmp_path, channels=channels, existing=both)
    assert result.returncode == 0, result.stderr
    assert "exists: projects/test-project/notificationChannels/42" in result.stdout
    assert "-X POST" not in calls and "policies create" not in calls
    assert result.stdout.count("exists, left as it is") == 2


@needs_tools
def test_a_wrong_metric_stops_the_script_before_anything_is_created(tmp_path):
    result, calls = run_script(tmp_path, descriptor='{"error": {"code": 404, "message": "Cannot find metric"}}')
    assert result.returncode != 0 and "was not found" in result.stderr
    assert "policies create" not in calls and "-X POST" not in calls
    result, _ = run_script(tmp_path, descriptor='{"type": "run.googleapis.com/job/completed_execution_count", "labels": [{"key": "job_name"}]}')
    assert result.returncode != 0  # the metric exists but has no 'result' label to tell failed from succeeded
