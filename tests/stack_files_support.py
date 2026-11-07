"""Stack tests whose Compose stand-in resolves companion files."""

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from app.compose_cli.binary import ComposeBinary
from app.services.stack_document import CompanionFile
from tests.stack_support import SCRIPTED_COMPOSE, Rig, SimCli
from tests.swarm_stack_support import SWARM_COMPOSE

FILES_COMPOSE = Path(__file__).with_name("files_compose.py")
OPERATION_TIMEOUT = 20.0


def plain(name: str, content: str = "A=1\n") -> CompanionFile:
    return CompanionFile(name, content)


def secret(name: str, content: str = "K=secret-value-1\n") -> CompanionFile:
    return CompanionFile(name, content, True)


async def use_files_compose(rig: Rig) -> None:
    """Run the rig's Compose stacks through the stand-in that resolves companion files."""
    rig.compose.use_binary(
        ComposeBinary((sys.executable, "-I", str(FILES_COMPOSE), str(SCRIPTED_COMPOSE)))
    )
    await rig.compose.detect()
    assert rig.compose.binary is not None
    sim = SimCli(
        rig.compose.binary,
        work_base=rig.compose.work_base,
        operation_timeout=OPERATION_TIMEOUT,
        host=rig.host,
    )
    rig.compose._cli = sim
    rig.sim = sim


def files_swarm_binary(directory: Path) -> ComposeBinary:
    directory.mkdir()
    script = directory / SWARM_COMPOSE.name
    shutil.copyfile(SWARM_COMPOSE, script)
    return ComposeBinary((sys.executable, "-I", str(FILES_COMPOSE), str(script)))


async def use_files_compose_for_swarm(rig: Rig, directory: Path) -> None:
    """Run the rig's Swarm stacks through the stand-in that resolves companion files."""
    rig.compose.use_binary(files_swarm_binary(directory))
    await rig.compose.detect()


def read_file_records(record: Path) -> list[dict[str, Any]]:
    """Return what the stand-in saw of each invocation's project directory, oldest first."""
    path = Path(f"{record}.files")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def invocation(record: Path, command: str, index: int = 0) -> dict[str, Any]:
    """Return the nth directory record of a command."""
    return [call for call in read_file_records(record) if call["cmd"] == command][index]
