"""Run the lint, type, and test checks in order, stopping at the first failure."""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Explicit paths keep each tool to the project's own directories.
STEPS: list[tuple[str, list[str]]] = [
    ("ruff check", ["-m", "ruff", "check", "app", "tests", "scripts", "updater"]),
    (
        "ruff format --check",
        ["-m", "ruff", "format", "--check", "app", "tests", "scripts", "updater"],
    ),
    ("mypy", ["-m", "mypy"]),
    ("pytest", ["-m", "pytest", "-n", "4", "--dist", "loadgroup"]),
]


def run_step(name: str, args: list[str]) -> int:
    """Run one check and return its exit code."""
    print(f"==> {name}", flush=True)
    started = time.perf_counter()
    code = subprocess.run([sys.executable, *args], cwd=ROOT, check=False).returncode
    elapsed = time.perf_counter() - started
    print(f"    {'ok' if code == 0 else 'FAILED'} ({elapsed:.1f}s)", flush=True)
    return code


def main() -> int:
    """Run every step; return the first non-zero exit code."""
    started = time.perf_counter()
    for name, args in STEPS:
        code = run_step(name, args)
        if code != 0:
            print(f"check failed at step: {name}", file=sys.stderr)
            return code
    print(f"all checks passed ({time.perf_counter() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
