"""The helper image carries no dependencies, so the program may import only the standard library."""

import ast
import sys
from pathlib import Path

HELPER_PACKAGE = "updater"
HELPER_DIRECTORY = Path(__file__).resolve().parents[2] / HELPER_PACKAGE
EXPECTED_MODULES = {"__init__", "__main__", "cli", "engine", "files", "plan", "result", "sequence"}


def imported_modules(source: str) -> set[str]:
    """Return the top-level names an absolute import in the source brings in."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def foreign_imports(source: str) -> set[str]:
    return imported_modules(source) - sys.stdlib_module_names - {HELPER_PACKAGE}


def test_every_module_of_the_helper_imports_only_the_standard_library_and_itself() -> None:
    sources = {p.stem: p.read_text(encoding="utf-8") for p in HELPER_DIRECTORY.glob("*.py")}

    foreign = {name: found for name, text in sources.items() if (found := foreign_imports(text))}
    assert set(sources) == EXPECTED_MODULES
    assert foreign == {}


def test_no_helper_module_uses_a_relative_import() -> None:
    sources = [path.read_text(encoding="utf-8") for path in HELPER_DIRECTORY.glob("*.py")]
    relative = [
        node
        for text in sources
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]

    assert relative == []


def test_the_scan_names_a_third_party_import_and_ignores_the_standard_library() -> None:
    source = (
        "import json\nimport httpx\nfrom pydantic.fields import Field\nfrom updater import cli\n"
    )

    assert foreign_imports(source) == {"httpx", "pydantic"}
