"""Guard: app.compose_cli is imported only by the services layer and imports nothing above it."""

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
WRAPPER = "app.compose_cli"
WRAPPER_DIR = APP_DIR / "compose_cli"
SERVICES_DIR = APP_DIR / "services"
LAYERS_ABOVE = (
    "app.services",
    "app.web",
    "app.api",
    "app.docker_engine",
    "app.db.repositories",
)


def imports_of(source: str, package: str) -> list[str]:
    """Return every import in source that reaches package, absolute or via its parent."""
    parent, _, leaf = package.rpartition(".")
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Import():
                found += [a.name for a in node.names if _within(a.name, package)]
            case ast.ImportFrom(module=module, level=0) if module is not None:
                if _within(module, package):
                    found.append(module)
                elif module == parent:
                    found += [f"{parent}.{a.name}" for a in node.names if a.name == leaf]
            case ast.ImportFrom(level=level) if level > 0:
                found.append(f"relative import (level {level}) cannot be checked")
            case ast.Call(
                func=ast.Name(id="__import__" | "import_module")
                | ast.Attribute(attr="import_module")
            ):
                found += [
                    arg.value
                    for arg in node.args
                    if isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and _within(arg.value, package)
                ]
    return found


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def is_inside(path: Path, directory: Path) -> bool:
    return directory in path.parents


@pytest.mark.parametrize(
    "source",
    [
        "import app.compose_cli",
        "import app.compose_cli.cli as cli",
        "from app.compose_cli import ComposeCli",
        "from app.compose_cli.cli import ComposeCli",
        "from app import compose_cli",
        "importlib.import_module('app.compose_cli.cli')",
        "__import__('app.compose_cli')",
    ],
)
def test_imports_of_flags_every_way_of_reaching_the_wrapper(source: str) -> None:
    assert len(imports_of(source, WRAPPER)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import app.services.stacks",
        "from app import services",
        "from app.compose_cli_other import thing",
        "message = 'app.compose_cli is internal'",
        "import compose_cli_helpers",
    ],
)
def test_imports_of_ignores_everything_else(source: str) -> None:
    assert imports_of(source, WRAPPER) == []


def test_relative_imports_are_flagged_because_they_cannot_be_checked() -> None:
    assert imports_of("from ..compose_cli import ComposeCli", WRAPPER) != []


@pytest.mark.parametrize(
    ("package", "source"),
    [
        ("app.services", "from app.services.stacks import StackService"),
        ("app.web", "import app.web.stack_routes"),
        ("app.api", "from app.api import stacks"),
        ("app.docker_engine", "from app.docker_engine.client import EngineClient"),
        ("app.db.repositories", "from app.db.repositories.environment import StatusRecord"),
    ],
)
def test_imports_of_flags_each_layer_the_wrapper_must_not_reach(package: str, source: str) -> None:
    assert len(imports_of(source, package)) == 1


@pytest.fixture(scope="module")
def importers() -> dict[Path, list[str]]:
    """Map each module outside the wrapper that imports it to what it imports."""
    found = {
        path: imports_of(path.read_text(encoding="utf-8"), WRAPPER)
        for path in sorted(APP_DIR.rglob("*.py"))
        if not is_inside(path, WRAPPER_DIR)
    }
    return {path: names for path, names in found.items() if names}


def test_the_services_layer_does_import_the_wrapper(importers: dict[Path, list[str]]) -> None:
    assert importers != {}


def test_the_wrapper_is_imported_only_from_the_services_layer(
    importers: dict[Path, list[str]],
) -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path, names in importers.items()
        if not is_inside(path, SERVICES_DIR)
        for name in names
    ]

    assert offenders == []


def test_the_wrapper_imports_nothing_from_the_layers_above_it() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path in sorted(WRAPPER_DIR.rglob("*.py"))
        for package in LAYERS_ABOVE
        for name in imports_of(path.read_text(encoding="utf-8"), package)
    ]

    assert offenders == []
