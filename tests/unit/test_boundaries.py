"""Boundary test: the domain layer imports neither FastAPI nor sqlite3 nor
httpx (discovery adapters do IO; the domain never does)."""

import ast
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "packages" / "domain"
FORBIDDEN = {"fastapi", "sqlite3", "httpx"}


def _imported_modules(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_domain_imports_no_io_library():
    files = list(DOMAIN_DIR.rglob("*.py"))
    assert files, "domain package not found"
    for f in files:
        forbidden = _imported_modules(f.read_text()) & FORBIDDEN
        assert not forbidden, f"{f} imports {forbidden}"
