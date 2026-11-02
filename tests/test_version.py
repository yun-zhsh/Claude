import re
from pathlib import Path

from valuation_monitor import __version__

ROOT = Path(__file__).parent.parent


def test_version_is_consistent_everywhere():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(rf'^version = "{re.escape(__version__)}"$', pyproject, re.M)
    assert f"v{__version__}" in (ROOT / "README.md").read_text(encoding="utf-8")  # README 开头写明当前版本
