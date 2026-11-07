"""The locks the image installs from agree with uv.lock and carry hashes."""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
BUILD_LOCK = ROOT / "build-requirements.lock"
UV_LOCK = ROOT / "uv.lock"
PYPROJECT = ROOT / "pyproject.toml"
ENTRY = re.compile(r"^([A-Za-z0-9._-]+)==(\S+)(?: ;[^\\]*)? \\$", re.MULTILINE)
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")


def normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(path: Path) -> dict[str, tuple[str, set[str]]]:
    """Return {name: (version, hashes)} for a requirements file with hashes."""
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=[A-Za-z0-9._-]+==)", text)
    parsed: dict[str, tuple[str, set[str]]] = {}
    for block in blocks:
        entry = ENTRY.match(block)
        if entry is not None:
            parsed[normalised(entry[1])] = (entry[2], set(HASH.findall(block)))
    return parsed


@pytest.fixture(scope="module")
def uv() -> dict[str, dict[str, Any]]:
    document = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return {normalised(package["name"]): package for package in document["package"]}


def hashes_of(package: dict[str, Any]) -> set[str]:
    found = {wheel["hash"].removeprefix("sha256:") for wheel in package.get("wheels", [])}
    if "sdist" in package:
        found.add(package["sdist"]["hash"].removeprefix("sha256:"))
    return found


def requirements_of(package: dict[str, Any], extras: list[str]) -> list[str]:
    names = [normalised(dep["name"]) for dep in package.get("dependencies", [])]
    optional = package.get("optional-dependencies", {})
    for extra in extras:
        names.extend(normalised(dep["name"]) for dep in optional.get(extra, []))
    return names


def runtime_closure(uv: dict[str, dict[str, Any]]) -> set[str]:
    seen: set[str] = set()
    pending = [
        (normalised(dep["name"]), dep.get("extra", []))
        for dep in uv["dockmin"].get("dependencies", [])
    ]
    while pending:
        name, extras = pending.pop()
        pending.extend(
            (dependency, [])
            for dependency in requirements_of(uv[name], extras)
            if dependency not in seen
        )
        seen.add(name)
    return seen


def test_the_lock_is_not_empty_and_every_entry_is_pinned_and_hashed() -> None:
    entries = parse(LOCK)

    assert len(entries) > 20
    assert [name for name, (_, hashes) in entries.items() if not hashes] == []


def test_every_pin_matches_the_version_uv_resolved(uv: dict[str, dict[str, Any]]) -> None:
    entries = parse(LOCK)

    mismatched = {
        name: (version, uv[name]["version"])
        for name, (version, _) in entries.items()
        if name not in uv or uv[name]["version"] != version
    }
    assert mismatched == {}


def test_every_hash_is_one_uv_recorded_for_that_release(uv: dict[str, dict[str, Any]]) -> None:
    entries = parse(LOCK)

    foreign = {name: hashes - hashes_of(uv[name]) for name, (_, hashes) in entries.items()}
    assert {name: extra for name, extra in foreign.items() if extra} == {}


def test_the_lock_holds_the_runtime_dependencies_and_nothing_else(
    uv: dict[str, dict[str, Any]],
) -> None:
    entries = set(parse(LOCK))

    assert entries == runtime_closure(uv)


def test_the_project_itself_and_the_development_tools_are_not_in_the_lock() -> None:
    entries = set(parse(LOCK))

    assert not entries & {"dockmin", "pytest", "ruff", "mypy", "pytest-asyncio"}


def test_the_build_lock_pins_the_build_backend_version_pyproject_asks_for() -> None:
    required = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["build-system"]["requires"]
    pinned = {
        normalised(REQUIREMENT_NAME.match(item)[0]): item.split("==")[1]  # type: ignore[index]
        for item in required
    }
    entries = parse(BUILD_LOCK)

    assert all(version == entries[name][0] for name, version in pinned.items())
    assert [name for name, (_, hashes) in entries.items() if not hashes] == []


def test_every_build_lock_hash_is_one_uv_knows_where_uv_resolves_the_package(
    uv: dict[str, dict[str, Any]],
) -> None:
    entries = parse(BUILD_LOCK)

    known = {name: hashes for name, (_, hashes) in entries.items() if name in uv}
    assert all(hashes <= hashes_of(uv[name]) for name, hashes in known.items())
