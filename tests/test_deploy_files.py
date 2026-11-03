"""The files used to build and deploy the container: they cannot be run here, but the obvious mistakes can be caught."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def patterns(path):
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def test_gcloudignore_and_dockerignore_exclude_the_same_things():
    """Both decide what goes into a build: the upload to Cloud Build, and the context of the docker build inside it."""
    assert patterns(ROOT / ".gcloudignore") == patterns(ROOT / ".dockerignore")
    assert {"data", "tests", ".git", ".venv"} <= set(patterns(ROOT / ".dockerignore"))  # the big and the useless stay out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_deploy_script_is_valid_bash_and_rejects_unknown_arguments():
    script = ROOT / "scripts" / "deploy-cloud.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    result = subprocess.run(["bash", str(script), "nonsense"], capture_output=True, text=True)
    assert result.returncode == 2 and "Usage" in result.stderr


def test_dockerfile_installs_dependencies_before_copying_the_code():
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    first_install = next(i for i, line in enumerate(lines) if "pip install" in line and "--no-deps" not in line)
    copy_code = next(i for i, line in enumerate(lines) if line.startswith("COPY src"))
    assert first_install < copy_code  # otherwise every code change reinstalls all the dependencies
    assert any("--no-deps" in line for line in lines[copy_code:])  # the real package goes in afterwards, without them


def test_cloud_build_config_reuses_the_previous_image_and_publishes_both_tags():
    text = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "--cache-from" in text and "${_IMAGE}:latest" in text and "${_IMAGE}:${_TAG}" in text


def test_the_public_mcp_service_is_separate_read_only_and_limited():
    text = (ROOT / "scripts" / "deploy-cloud.sh").read_text(encoding="utf-8")
    mcp_block = text[text.index('if [ "$WHAT" = mcp ]; then') :]
    before = text[: text.index('if [ "$WHAT" = mcp ]; then')]
    assert "--allow-unauthenticated" in mcp_block and text.count("--allow-unauthenticated") == 1  # nothing else is public
    assert "--no-allow-unauthenticated" in before  # the web page stays behind IAP
    assert 'if [ "$WHAT" = all ] || [ "$WHAT" = job ]' in before and "= mcp" not in before.split("phase \"Image repository\"")[1]  # "all" does not deploy the public service
    assert "roles/storage.objectViewer" in mcp_block and "objectAdmin" not in text and "storage.admin" not in text  # the account can only read
    assert "readonly=true" in mcp_block  # the filings are mounted read-only
    assert "--max-instances" in mcp_block and "--concurrency" in mcp_block and "--timeout 60" in mcp_block
    assert "SEC_USER_AGENT" not in mcp_block and "VALUATION_UPDATE_JOB" not in mcp_block  # no secret, no right to start the update job


def test_the_image_contains_the_mcp_library():
    assert "'.[cloud,mcp]'" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
