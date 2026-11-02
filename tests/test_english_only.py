"""The software is English-only at runtime: users never see Chinese (or any other CJK text), including stock names.

The only place allowed to contain Chinese is src/valuation_monitor/cn_patterns.py: the patterns used to *read* the
Chinese-language filings of A-share companies. Those patterns are never displayed.
Code comments, docstrings and tests may still be written in Chinese.
"""

import ast
import re
from pathlib import Path

import pytest

from tests.test_web import client, monitor  # noqa: F401  (fixtures)
from valuation_monitor.web import export_site

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "valuation_monitor"
CJK = re.compile("[　-〿぀-ヿ㐀-鿿가-힯＀-￯]")
ALLOWED_PYTHON = {SRC / "cn_patterns.py"}


def docstring_nodes(tree):
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                nodes.add(id(first.value))
    return nodes


def test_python_string_literals_have_no_cjk():
    """Every string literal that could reach the screen, a file or a message (docstrings and comments are not output)."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path in ALLOWED_PYTHON:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip and CJK.search(node.value):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value[:40]!r}")
    assert not offenders, "Chinese text in program output:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "path",
    [*sorted(SRC.glob("web/templates/*.html")), *sorted(SRC.glob("web/static/*.js")), *sorted(SRC.glob("web/static/*.css")),
     ROOT / "config.yaml", ROOT / ".github" / "workflows" / "pages.yml"],
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_templates_static_files_and_config_have_no_cjk(path):
    if path.name == "chart.umd.min.js":
        pytest.skip("third-party library")
    lines = [f"{n}: {line.strip()[:60]}" for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if CJK.search(line)]
    assert not lines, "\n".join(lines)


def test_saved_data_files_have_no_cjk():
    offenders = []
    for path in sorted((ROOT / "data").rglob("*")):
        if path.suffix in (".csv", ".json") and path.is_file():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if CJK.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}")
                    break
    assert not offenders, offenders


def test_rendered_pages_and_downloads_have_no_cjk(client, monitor, tmp_path):  # noqa: F811
    pages = ["/", "/stock/0700.HK/"] + [f"/stock/0700.HK/download/{n}" for n in ("prices.csv", "eps.csv", "ttm.csv", "valuation.csv")]
    for url in pages:
        r = client.get(url)
        assert r.status_code == 200, url
        text = r.data.decode("utf-8-sig")
        assert not CJK.search(text), f"{url}: {CJK.search(text).group()!r}"
        assert not CJK.search(r.headers.get("Content-Disposition", "")), url
    out = tmp_path / "site"
    export_site(monitor, out, base_path="/x")
    for page in out.rglob("*.html"):
        assert not CJK.search(page.read_text(encoding="utf-8")), page

