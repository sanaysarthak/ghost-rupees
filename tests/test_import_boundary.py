"""
The one architectural rule: core/ must never import from llm/.

This is the deterministic engine's safety boundary - the LLM must never
be able to reach the money path. Enforced by walking the AST of every
file under core/, not by convention.
"""

import ast
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "core"


def _imports_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def test_core_never_imports_llm():
    violations = []
    for py_file in CORE_DIR.rglob("*.py"):
        for name in _imports_in(py_file):
            if name == "llm" or name.startswith("llm."):
                violations.append(f"{py_file.relative_to(CORE_DIR.parent)} imports {name!r}")
    assert not violations, "core/ must never import llm/:\n" + "\n".join(violations)


def test_core_dir_actually_has_files_to_check():
    # guards against this test silently passing because CORE_DIR was empty/misnamed
    py_files = list(CORE_DIR.rglob("*.py"))
    assert len(py_files) >= 5
