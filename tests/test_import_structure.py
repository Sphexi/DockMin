"""Guards on the import graph of app/: no cycles, and the launcher is a leaf."""

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
ENTRY_POINTS = ("app.main", "app.serving.launcher")
SERVING = APP_DIR / "serving"


def module_name(path: Path) -> str:
    relative = path.relative_to(APP_DIR.parent).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(parts)


def modules() -> dict[str, Path]:
    return {module_name(path): path for path in APP_DIR.rglob("*.py")}


def runtime_imports(source: str, known: set[str]) -> set[str]:
    """Return the app modules imported when the module loads: not in a function or TYPE_CHECKING."""
    found: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            match node:
                case ast.Import():
                    found.update(a.name for a in node.names if a.name in known)
                case ast.ImportFrom(module=module, level=0) if module is not None:
                    for alias in node.names:
                        candidate = f"{module}.{alias.name}"
                        if candidate in known:
                            found.add(candidate)
                        elif module in known:
                            found.add(module)
                case ast.If(test=ast.Name(id="TYPE_CHECKING")):
                    visit(node.orelse)
                case ast.If() | ast.Try() | ast.With() | ast.For() | ast.While():
                    for field in ("body", "orelse", "finalbody"):
                        visit(getattr(node, field, []))
                    for handler in getattr(node, "handlers", []):
                        visit(handler.body)

    visit(ast.parse(source).body)
    return found


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return every strongly connected group of more than one module, and every self-import."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    groups: list[list[str]] = []
    counter = 0

    def connect(start: str) -> None:
        nonlocal counter
        work: list[tuple[str, Iterator[str]]] = [(start, iter(sorted(graph.get(start, ()))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, neighbours = work[-1]
            for neighbour in neighbours:
                if neighbour not in index:
                    index[neighbour] = low[neighbour] = counter
                    counter += 1
                    stack.append(neighbour)
                    on_stack.add(neighbour)
                    work.append((neighbour, iter(sorted(graph.get(neighbour, ())))))
                    break
                if neighbour in on_stack:
                    low[node] = min(low[node], index[neighbour])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    group = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        group.append(member)
                        if member == node:
                            break
                    if len(group) > 1 or node in graph.get(node, ()):
                        groups.append(sorted(group))

    for node in sorted(graph):
        if node not in index:
            connect(node)
    return groups


def test_the_cycle_finder_reports_a_cycle_and_a_self_import_and_ignores_a_chain() -> None:
    graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"e"}, "e": set(), "f": {"f"}}

    assert cycles(graph) == [["a", "b", "c"], ["f"]]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import app.x", {"app.x"}, id="plain"),
        pytest.param("from app import x", {"app.x"}, id="from-package-module"),
        pytest.param("from app.x import name", {"app.x"}, id="from-module-name"),
        pytest.param("def f():\n    import app.x\n", set(), id="inside-a-function"),
        pytest.param(
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import app.x\n",
            set(),
            id="type-checking-only",
        ),
        pytest.param(
            "try:\n    import app.x\nexcept ImportError:\n    pass\n", {"app.x"}, id="try"
        ),
    ],
)
def test_only_imports_that_run_when_the_module_loads_are_counted(
    source: str, expected: set[str]
) -> None:
    assert runtime_imports(source, {"app", "app.x"}) == expected


def test_no_module_of_the_application_is_part_of_an_import_cycle() -> None:
    known = modules()
    names = set(known)
    graph = {
        name: runtime_imports(path.read_text(encoding="utf-8"), names) - {name}
        for name, path in known.items()
    }

    assert cycles(graph) == []


def test_only_the_serving_package_imports_the_application_factory_or_the_launcher() -> None:
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        if SERVING in path.parents:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            if any(
                name in ENTRY_POINTS or name.startswith("app.serving.launcher") for name in names
            ):
                offenders.append(f"{path.relative_to(APP_DIR)}:{cast(ast.stmt, node).lineno}")
    assert offenders == []
