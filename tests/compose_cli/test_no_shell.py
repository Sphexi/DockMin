"""Guard: nothing in app/ starts a shell, and only the Compose process module starts processes."""

import ast
import functools
import itertools
import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"
PROCESS_MODULE = APP_DIR / "compose_cli" / "process.py"
SHELL_NAMES = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "/bin/sh", "/bin/bash", "/usr/bin/env"}
)
SHELL_COMMAND_TEXT = re.compile(r"\b(?:ba|z|da|k)?sh\s+-c\b")
SHELL_CALLS = frozenset({"system", "popen", "create_subprocess_shell", "getoutput"})
OS_SPAWN_CALLS = frozenset({"fork", "forkpty", "posix_spawn", "posix_spawnp"})
SPAWN_MODULES = frozenset({"subprocess", "pty", "multiprocessing"})
DYNAMIC_IMPORTS = frozenset({"__import__", "import_module"})


@functools.cache
def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def shell_hazards(source: str) -> list[int]:
    """Return the line of every construct that runs text through a shell."""
    lines: list[int] = []
    for node in ast.walk(_tree(source)):
        match node:
            case ast.keyword(arg="shell", value=value) if not _is_false(value):
                lines.append(value.lineno)
            case ast.Call(func=ast.Attribute(attr=name) | ast.Name(id=name)) if name in SHELL_CALLS:
                lines.append(node.lineno)
            case (
                ast.Attribute(attr="create_subprocess_shell")
                | ast.Name(id="create_subprocess_shell")
            ):
                lines.append(node.lineno)
            case ast.Constant(value=str() as text) if SHELL_COMMAND_TEXT.search(text):
                lines.append(node.lineno)
            case ast.List(elts=items) | ast.Tuple(elts=items) if _starts_a_shell(items):
                lines.append(node.lineno)
    return lines


def spawn_uses(source: str) -> list[int]:
    """Return the line of every construct that starts or imports a way to start a process."""
    lines: list[int] = []
    for node in ast.walk(_tree(source)):
        match node:
            case ast.Import(names=names) if any(_is_spawn_module(a.name) for a in names):
                lines.append(node.lineno)
            case ast.ImportFrom(module=str() as module, level=0) if _is_spawn_module(module):
                lines.append(node.lineno)
            case ast.Call(func=ast.Name(id=name) | ast.Attribute(attr=name)) if (
                name == "create_subprocess_exec"
            ):
                lines.append(node.lineno)
            case ast.Call(func=ast.Attribute(value=ast.Name(id="os"), attr=name)) if (
                name in OS_SPAWN_CALLS or name.startswith(("exec", "spawn"))
            ):
                lines.append(node.lineno)
            case ast.Call(
                func=ast.Name(id=name), args=[ast.Constant(value=str() as module), *_]
            ) if name in DYNAMIC_IMPORTS and _is_spawn_module(module):
                lines.append(node.lineno)
            case ast.Call(
                func=ast.Attribute(attr="import_module"),
                args=[ast.Constant(value=str() as module), *_],
            ) if _is_spawn_module(module):
                lines.append(node.lineno)
    return lines


def _is_false(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is False


def _is_spawn_module(name: str) -> bool:
    return name.split(".")[0] in SPAWN_MODULES


def _starts_a_shell(items: list[ast.expr]) -> bool:
    texts = [i.value if isinstance(i, ast.Constant) else None for i in items]
    return any(
        first in SHELL_NAMES and following in {"-c", "-lc", "/c", "-Command"}
        for first, following in itertools.pairwise(texts)
    )


@pytest.mark.parametrize(
    "source",
    [
        "subprocess.run(command, shell=True)",
        "subprocess.Popen(command, shell=flag)",
        "os.system('ls ' + name)",
        "os.popen(command)",
        "subprocess.getoutput(command)",
        "await asyncio.create_subprocess_shell(command)",
        "start = asyncio.create_subprocess_shell",
        "argv = ['sh', '-c', command]",
        "argv = ('/bin/bash', '-c', command)",
        "command = 'sh -c \"docker compose up\"'",
        "command = f'bash -c {script}'",
    ],
)
def test_shell_hazards_flags_every_way_of_running_text_through_a_shell(source: str) -> None:
    assert shell_hazards(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "subprocess.run(argv, shell=False)",
        "await asyncio.create_subprocess_exec(*argv)",
        "argv = ['docker', 'compose', 'up']",
        "argv = ['sh']",
        "message = 'push -c is not a shell'",
        "label = 'Run in a shell'",
        "shell = False",
        "system = 'linux'",
    ],
)
def test_shell_hazards_ignores_everything_else(source: str) -> None:
    assert shell_hazards(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "import pty",
        "from multiprocessing import Pool",
        "await asyncio.create_subprocess_exec(*argv)",
        "proc = create_subprocess_exec(*argv)",
        "os.execv(path, argv)",
        "os.spawnl(os.P_WAIT, path)",
        "os.fork()",
        "os.posix_spawn(path, argv, env)",
        "__import__('subprocess')",
        "importlib.import_module('subprocess')",
    ],
)
def test_spawn_uses_flags_every_way_of_starting_a_process(source: str) -> None:
    assert spawn_uses(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import asyncio",
        "import os",
        "os.environ.get('HOME')",
        "os.path.join(a, b)",
        "message = 'subprocess'",
        "stdout = asyncio.subprocess.PIPE",
        "result = runner.run(task)",
        "importlib.import_module('app.services')",
    ],
)
def test_spawn_uses_ignores_everything_else(source: str) -> None:
    assert spawn_uses(source) == []


def test_no_module_of_the_application_runs_text_through_a_shell() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}:{line}"
        for path in sorted(APP_DIR.rglob("*.py"))
        for line in shell_hazards(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_only_the_compose_process_module_starts_processes() -> None:
    spawning = {
        path
        for path in sorted(APP_DIR.rglob("*.py"))
        if spawn_uses(path.read_text(encoding="utf-8"))
    }

    assert spawning == {PROCESS_MODULE}
