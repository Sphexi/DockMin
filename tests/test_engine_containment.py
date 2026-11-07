"""Guard: app.docker_engine may be imported only from the services layer and the database layer."""

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
ENGINE = "app.docker_engine"
ALLOWED = (
    APP_DIR / "services",
    APP_DIR / "subsystems",
    APP_DIR / "db",
    APP_DIR / "docker_engine",
)


def engine_imports(source: str) -> list[str]:
    """Return every import in source that reaches app.docker_engine, absolute or via app."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Import():
                found += [a.name for a in node.names if _is_engine(a.name)]
            case ast.ImportFrom(module=module, level=0) if module is not None:
                if _is_engine(module):
                    found.append(module)
                elif module == "app":
                    found += [f"app.{a.name}" for a in node.names if _is_engine(f"app.{a.name}")]
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
                    and _is_engine(arg.value)
                ]
    return found


def _is_engine(module: str) -> bool:
    return module == ENGINE or module.startswith(f"{ENGINE}.")


def is_allowed(path: Path) -> bool:
    return any(allowed in path.parents for allowed in ALLOWED)


@pytest.mark.parametrize(
    "source",
    [
        "import app.docker_engine",
        "import app.docker_engine.client as c",
        "from app.docker_engine import EngineClient",
        "from app.docker_engine.transport import create_transport",
        "from app import docker_engine",
        "importlib.import_module('app.docker_engine.client')",
        "__import__('app.docker_engine')",
    ],
)
def test_engine_imports_flags_every_way_of_reaching_the_engine(source: str) -> None:
    assert len(engine_imports(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import app.services.access",
        "from app.db import Database",
        "from app import services",
        "message = 'app.docker_engine is internal'",
        "import docker_engine_helpers",
    ],
)
def test_engine_imports_ignores_everything_else(source: str) -> None:
    assert engine_imports(source) == []


def test_relative_imports_are_flagged_because_they_cannot_be_checked() -> None:
    assert engine_imports("from ..docker_engine import EngineClient") != []


def test_app_docker_engine_is_imported_only_from_services_subsystems_and_db() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if not is_allowed(path)
        for name in engine_imports(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def http_layer_uses(source: str) -> list[int]:
    """Return the line of every access to an `_http` attribute, direct or by getattr."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Attribute(attr="_http"):
                lines.append(node.lineno)
            case ast.Call(func=ast.Name(id="getattr"), args=[_, ast.Constant(value="_http"), *_]):
                lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    "source",
    [
        "client._http.request('POST', '/x')",
        "http = self._client._http",
        "getattr(client, '_http')",
    ],
)
def test_http_layer_uses_flags_every_way_of_reaching_the_http_layer(source: str) -> None:
    assert len(http_layer_uses(source)) == 1


@pytest.mark.parametrize(
    "source",
    ["client.containers.stop(cid)", "http = self._http_client", "message = 'client._http'"],
)
def test_http_layer_uses_ignores_everything_else(source: str) -> None:
    assert http_layer_uses(source) == []


def test_the_http_layer_is_used_only_inside_the_engine_package() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}:{line}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if APP_DIR / "docker_engine" not in path.parents
        for line in http_layer_uses(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


REGISTRY_CLIENT = "app.registry_client"
REGISTRY_CLIENT_ALLOWED = (
    APP_DIR / "services",
    APP_DIR / "db",
    APP_DIR / "registry_client",
    APP_DIR / "object_storage",
    APP_DIR / "notifications",
)


def registry_client_imports(source: str) -> list[str]:
    """Return every import in source that reaches app.registry_client, absolute or via app."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Import():
                found += [a.name for a in node.names if _is_registry_client(a.name)]
            case ast.ImportFrom(module=module, level=0) if module is not None:
                if _is_registry_client(module):
                    found.append(module)
                elif module == "app":
                    found += [
                        f"app.{a.name}" for a in node.names if _is_registry_client(f"app.{a.name}")
                    ]
            case ast.ImportFrom(level=level) if level > 0:
                found.append(f"relative import (level {level}) cannot be checked")
    return found


def _is_registry_client(module: str) -> bool:
    return module == REGISTRY_CLIENT or module.startswith(f"{REGISTRY_CLIENT}.")


@pytest.mark.parametrize(
    "source",
    [
        "import app.registry_client",
        "import app.registry_client.probe as p",
        "from app.registry_client import RegistryTarget",
        "from app.registry_client.ecr import EcrTokenSource",
        "from app import registry_client",
    ],
)
def test_registry_client_imports_flags_every_way_of_reaching_the_registry_client(
    source: str,
) -> None:
    assert len(registry_client_imports(source)) == 1


@pytest.mark.parametrize(
    "source",
    ["import app.services.registries", "from app import services", "import app.netcheck"],
)
def test_registry_client_imports_ignores_everything_else(source: str) -> None:
    assert registry_client_imports(source) == []


def test_app_registry_client_is_imported_only_from_services_db_and_itself() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if not any(allowed in path.parents for allowed in REGISTRY_CLIENT_ALLOWED)
        for name in registry_client_imports(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_registry_client_and_the_shared_network_checks_never_reach_the_engine_layer() -> None:
    paths = [*sorted((APP_DIR / "registry_client").rglob("*.py")), APP_DIR / "netcheck.py"]

    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path in paths
        for name in engine_imports(path.read_text(encoding="utf-8"))
    ]

    assert paths
    assert offenders == []


def test_the_shared_network_checks_do_not_depend_on_either_client() -> None:
    source = (APP_DIR / "netcheck.py").read_text(encoding="utf-8")

    assert engine_imports(source) == []
    assert registry_client_imports(source) == []


OBJECT_STORAGE = "app.object_storage"
OBJECT_STORAGE_ALLOWED = (APP_DIR / "services", APP_DIR / "object_storage")


def object_storage_imports(source: str) -> list[str]:
    """Return every import in source that reaches app.object_storage, absolute or via app."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Import():
                found += [a.name for a in node.names if _is_object_storage(a.name)]
            case ast.ImportFrom(module=module, level=0) if module is not None:
                if _is_object_storage(module):
                    found.append(module)
                elif module == "app":
                    found += [
                        f"app.{a.name}" for a in node.names if _is_object_storage(f"app.{a.name}")
                    ]
            case ast.ImportFrom(level=level) if level > 0:
                found.append(f"relative import (level {level}) cannot be checked")
    return found


def _is_object_storage(module: str) -> bool:
    return module == OBJECT_STORAGE or module.startswith(f"{OBJECT_STORAGE}.")


@pytest.mark.parametrize(
    "source",
    [
        "import app.object_storage",
        "import app.object_storage.client as c",
        "from app.object_storage import ObjectStorageClient",
        "from app.object_storage.errors import StorageFailure",
        "from app import object_storage",
    ],
)
def test_object_storage_imports_flags_every_way_of_reaching_the_client(source: str) -> None:
    assert len(object_storage_imports(source)) == 1


@pytest.mark.parametrize(
    "source",
    ["import app.services.object_storage", "from app import services", "import app.netcheck"],
)
def test_object_storage_imports_ignores_everything_else(source: str) -> None:
    assert object_storage_imports(source) == []


def test_app_object_storage_is_imported_only_from_services_and_itself() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if not any(allowed in path.parents for allowed in OBJECT_STORAGE_ALLOWED)
        for name in object_storage_imports(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_object_storage_client_never_reaches_the_engine_layer() -> None:
    paths = sorted((APP_DIR / "object_storage").rglob("*.py"))

    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path in paths
        for name in engine_imports(path.read_text(encoding="utf-8"))
    ]

    assert paths
    assert offenders == []


BACKUP = "app.backup"
BACKUP_ALLOWED = (APP_DIR / "services", APP_DIR / "subsystems", APP_DIR / "backup")
BACKUP_FORBIDDEN_DEPENDENCIES = (
    "app.web",
    "app.api",
    "app.docker_engine",
    "app.compose_cli",
)


def _is_backup(module: str) -> bool:
    return module == BACKUP or module.startswith(f"{BACKUP}.")


def module_imports(source: str, matches: Callable[[str], bool]) -> list[str]:
    """Return every import in source of a module that matches, absolute or via app."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Import():
                found += [a.name for a in node.names if matches(a.name)]
            case ast.ImportFrom(module=module, level=0) if module is not None:
                if matches(module):
                    found.append(module)
                elif module == "app":
                    found += [f"app.{a.name}" for a in node.names if matches(f"app.{a.name}")]
            case ast.ImportFrom(level=level) if level > 0:
                found.append(f"relative import (level {level}) cannot be checked")
    return found


def _is_forbidden_for_backup(module: str) -> bool:
    return any(
        module == name or module.startswith(f"{name}.") for name in BACKUP_FORBIDDEN_DEPENDENCIES
    )


@pytest.mark.parametrize(
    "source",
    [
        "import app.backup",
        "import app.backup.archive as a",
        "from app.backup import produce_archive",
        "from app.backup.manifest import Manifest",
        "from app import backup",
    ],
)
def test_backup_imports_flags_every_way_of_reaching_the_archive_library(source: str) -> None:
    assert len(module_imports(source, _is_backup)) == 1


@pytest.mark.parametrize(
    "source",
    ["import app.services.backups", "from app import services", "import app.backup_helpers"],
)
def test_backup_imports_ignores_everything_else(source: str) -> None:
    assert module_imports(source, _is_backup) == []


def test_app_backup_is_imported_only_from_services_subsystems_and_itself() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if not any(allowed in path.parents for allowed in BACKUP_ALLOWED)
        for name in module_imports(path.read_text(encoding="utf-8"), _is_backup)
    ]

    assert offenders == []


def test_the_archive_library_never_reaches_the_web_api_engine_or_compose_layers() -> None:
    paths = sorted((APP_DIR / "backup").rglob("*.py"))

    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {name}"
        for path in paths
        for name in module_imports(path.read_text(encoding="utf-8"), _is_forbidden_for_backup)
    ]

    assert paths
    assert offenders == []
