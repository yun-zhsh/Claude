"""The GitHub Actions workflows: the tests gate the publication, so keep that wiring from being removed by accident."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(workflow):
    return workflow.get("on") or workflow[True]  # YAML 1.1 reads a bare `on` as the boolean True


def test_tests_run_on_pull_requests_and_can_be_called_by_the_publish_workflow():
    ci = load("ci.yml")
    assert {"pull_request", "workflow_call"} <= set(triggers(ci))
    assert set(ci["jobs"]) == {"test", "image"}
    assert ci["jobs"]["test"]["strategy"]["matrix"]["python"] == ["3.10", "3.12"]  # the oldest and the newest supported


def test_nothing_is_published_after_a_code_change_unless_the_tests_pass():
    pages = load("pages.yml")
    tests, build = pages["jobs"]["tests"], pages["jobs"]["build"]
    assert tests["uses"] == "./.github/workflows/ci.yml" and tests["if"] == "github.event_name != 'schedule'"  # scheduled runs reuse code that passed
    assert build["needs"] == "tests"
    condition = build["if"]
    assert "needs.tests.result == 'success'" in condition and "needs.tests.result == 'skipped'" in condition  # not "always run"
    assert pages["jobs"]["deploy"]["needs"] == "build"  # a skipped build skips the deployment


def test_the_database_size_check_runs_before_the_data_is_committed():
    steps = [step.get("name", "") for step in load("pages.yml")["jobs"]["build"]["steps"]]
    assert steps.index("Check the size of the database") < steps.index("Commit the updated data")


def test_stale_data_still_publishes_but_fails_the_run_at_the_end():
    pages = load("pages.yml")
    build_steps = [step.get("name", step.get("uses", "")) for step in pages["jobs"]["build"]["steps"]]
    assert build_steps.index("Check that the data is fresh") < build_steps.index("actions/upload-pages-artifact@v3")
    check = next(step for step in pages["jobs"]["build"]["steps"] if step.get("name") == "Check that the data is fresh")
    assert "valuation-monitor-check" in check["run"] and "continue-on-error" not in check  # a stale series must not stop the publication...
    assert pages["jobs"]["build"]["outputs"]["stale"] == "${{ steps.freshness.outputs.stale }}"
    last = pages["jobs"]["deploy"]["steps"][-1]  # ...but after it has happened, the run fails
    assert last["name"] == "Fail the run if the data is stale" and last["if"] == "needs.build.outputs.stale == 'true'" and "exit 1" in last["run"]
