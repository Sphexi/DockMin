"""Recording stand-in for the Compose binary; replays committed samples chosen by FAKE_SCENARIO.

Run as `python -I fake_compose.py <compose argv>`. FAKE_RECORD names a JSON Lines file that gets
one record per invocation, taken while the process runs. FAKE_STDERR_RAW is printed to stderr
after the replay, FAKE_EXIT overrides the exit code, FAKE_PIDS receives "<pid> <grandchild pid>".
"""

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_FLAGS_WITH_VALUE = 2
HANG_SECONDS = 60
CHUNK = b"x" * 65536
BIG_CHUNKS = 32
LONG_LINE_BYTES = 200 * 1024
NOISY_EVENTS = 600

STDOUT_BY_COMMAND = {
    "version": "version.stdout",
    "config": "config-local.stdout",
    "ps": "ps-local.stdout",
}
STDERR_BY_COMMAND = {
    "up": "up-adopt-both.stderr",
    "start": "up-local.stderr",
    "down": "down-local.stderr",
    "stop": "down-local.stderr",
}
STDOUT_EVENT = '{"id":"Container dockmin-e2e-sample-web-1","status":"Done","text":"Started"}'


def command_of(argv: list[str]) -> str:
    if "--env-file" not in argv:
        return argv[0]
    position = argv.index("--env-file") + PROJECT_FLAGS_WITH_VALUE
    if argv[position] == "--profile":
        position += PROJECT_FLAGS_WITH_VALUE
    return argv[position]


def mode_of(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))


def record(argv: list[str]) -> None:
    target = os.environ.get("FAKE_RECORD")
    if not target:
        return
    workdir = Path(os.environ["HOME"]).parent
    cert_path = os.environ.get("DOCKER_CERT_PATH")
    env_file = Path(argv[argv.index("--env-file") + 1]) if "--env-file" in argv else None
    entry = {
        "argv": argv,
        "env": dict(os.environ),
        "cwd": str(Path.cwd()),
        "workdir": str(workdir),
        "workdir_mode": mode_of(workdir),
        "entries": {child.name: mode_of(child) for child in sorted(workdir.iterdir())},
        "tls": (
            {f.name: mode_of(f) for f in sorted(Path(cert_path).iterdir())} if cert_path else {}
        ),
        "env_file_size": env_file.stat().st_size if env_file else None,
        "docker_config": Path(os.environ["DOCKER_CONFIG"], "config.json").read_text(
            encoding="utf-8"
        ),
    }
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def replay(command: str, scenario: str) -> None:
    if scenario == "fail":
        sys.stderr.write((FIXTURES / "up-fail-local.stderr").read_text(encoding="utf-8"))
    elif command in STDOUT_BY_COMMAND:
        sys.stdout.write((FIXTURES / STDOUT_BY_COMMAND[command]).read_text(encoding="utf-8"))
    elif command in STDERR_BY_COMMAND:
        sys.stderr.write((FIXTURES / STDERR_BY_COMMAND[command]).read_text(encoding="utf-8"))
        sys.stdout.write(STDOUT_EVENT + "\n")
    raw = os.environ.get("FAKE_STDERR_RAW")
    if raw:
        sys.stderr.write(raw + "\n")


def hang(*, ignore_sigterm: bool) -> None:
    code = f"import time\ntime.sleep({HANG_SECONDS})"
    if ignore_sigterm:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        code = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + code
    child = subprocess.Popen([sys.executable, "-I", "-c", code])
    Path(os.environ["FAKE_PIDS"]).write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
    sys.stderr.write('{"id":"Container dockmin-e2e-sample-web-1","status":"Working"}\n')
    sys.stderr.flush()
    time.sleep(HANG_SECONDS)


def hold_pipes() -> None:
    code = f"import time\ntime.sleep({HANG_SECONDS})"
    child = subprocess.Popen([sys.executable, "-I", "-c", code])
    Path(os.environ["FAKE_PIDS"]).write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
    sys.stderr.write('{"id":"Container dockmin-e2e-sample-web-1","status":"Done"}\n')
    sys.stderr.flush()


def main() -> int:
    argv = sys.argv[1:]
    command = command_of(argv)
    scenario = os.environ.get("FAKE_SCENARIO", "ok")
    record(argv)
    match scenario:
        case "hang" | "hang_ignore":
            hang(ignore_sigterm=scenario == "hang_ignore")
        case "pipeheld":
            hold_pipes()
        case "big":
            for _ in range(BIG_CHUNKS):
                sys.stdout.buffer.write(CHUNK)
            sys.stdout.buffer.flush()
        case "noisy":
            sys.stderr.write("y" * LONG_LINE_BYTES + "\n")
            for number in range(NOISY_EVENTS):
                sys.stderr.write(
                    f'{{"id":"Container c{number}","status":"Done","text":"Started"}}\n'
                )
        case _:
            replay(command, scenario)
    return int(os.environ.get("FAKE_EXIT", "1" if scenario == "fail" else "0"))


if __name__ == "__main__":
    sys.exit(main())
