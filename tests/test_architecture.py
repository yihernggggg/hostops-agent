"""Guardrail 3, checked a second way: agent and governance code never import the service.

Ruff's banned-api rule catches `import hostops.service`. This test also catches
relative imports and `from hostops import service`, which a name-based lint can miss.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hostops"
GUARDED = [*sorted((ROOT / "agent").glob("*.py")), ROOT / "governance.py"]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # relative import: resolve against the package path
                package = path.relative_to(ROOT.parent).with_suffix("").parts[: -node.level]
                module = ".".join([*package, module]) if module else ".".join(package)
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def test_guarded_files_exist() -> None:
    assert (ROOT / "governance.py").exists()
    assert (ROOT / "agent" / "loop.py").exists()


def test_agent_and_governance_never_import_service() -> None:
    offenders = {
        str(p.relative_to(ROOT.parent)): sorted(
            m for m in _imported_modules(p) if m.startswith("hostops.service")
        )
        for p in GUARDED
    }
    assert {k: v for k, v in offenders.items() if v} == {}
