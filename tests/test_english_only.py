"""The project is English-only: users never see Chinese (or any other CJK text), including stock names, and developers
see English only in code comments, docstrings, tests and documentation.

The only places allowed to contain Chinese are string literals that *read* Chinese-language filings of A-share
companies: src/valuation_monitor/cn_patterns.py and the fixtures in tests/test_cn.py. Their comments and docstrings
must still be English.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

from tests.test_web import client, monitor  # noqa: F401  (fixtures)
from valuation_monitor.web import export_site

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "valuation_monitor"
CJK = re.compile("[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef]")
ALLOWED_PYTHON = {SRC / "cn_patterns.py"}
ALLOWED_LITERALS = ALLOWED_PYTHON | {ROOT / "tests" / "test_cn.py"}  # Chinese string fixtures of the source filings


def docstring_nodes_of(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) in docstring_nodes(tree)]


def docstring_nodes(tree):
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                nodes.add(id(first.value))
    return nodes


def python_files():
    return [*sorted(SRC.rglob("*.py")), *sorted((ROOT / "tests").glob("*.py"))]


def test_python_comments_and_docstrings_have_no_cjk():
    offenders = []
    for path in python_files():
        source = path.read_text(encoding="utf-8")
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and CJK.search(tok.string):
                offenders.append(f"{path.relative_to(ROOT)}:{tok.start[0]}: {tok.string[:50]!r}")
        tree = ast.parse(source)
        for node in docstring_nodes_of(tree):
            if CJK.search(node.value):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: docstring")
    assert not offenders, "Chinese text in comments or docstrings:\n" + "\n".join(offenders)


def test_documentation_has_no_cjk():
    docs = [ROOT / "README.md", ROOT / "CLAUDE.md", ROOT / "pyproject.toml", ROOT / "Dockerfile", ROOT / ".dockerignore", ROOT / ".gcloudignore", ROOT / "cloudbuild.yaml",
            *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "scripts").glob("*"))]
    offenders = [f"{p.relative_to(ROOT)}:{n}" for p in docs if p.exists() for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if CJK.search(line)]
    assert not offenders, offenders


def test_python_string_literals_have_no_cjk():
    """Every string literal that could reach the screen, a file or a message (docstrings and comments are not output)."""
    offenders = []
    for path in python_files():
        if path in ALLOWED_LITERALS or path.name == "test_english_only.py":
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
     ROOT / "config.yaml", ROOT / ".github" / "workflows" / "pages.yml", ROOT / ".github" / "workflows" / "ci.yml"],
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


def test_saved_database_has_no_cjk():
    """data/valuation.db is binary, so the text cells of every table are checked here."""
    import sqlite3

    path = ROOT / "data" / "valuation.db"
    if not path.exists():
        pytest.skip("no saved database")
    offenders = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                if any(isinstance(v, str) and CJK.search(v) for v in row):
                    offenders.append(f"{table}: {[v for v in row if isinstance(v, str) and CJK.search(v)][0][:40]!r}")
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

