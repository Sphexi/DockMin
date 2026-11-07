"""A Compose stand-in that resolves companion files the way Compose does.

Runs the stand-in named by the first argument and rewrites what `config` prints: environment and
label files are merged in, and a config or secret read from a file becomes an environment source.
"""

import contextlib
import io
import json
import runpy
import sys
from pathlib import Path
from typing import Any

import yaml

BASE = sys.argv[1]
sys.argv = [BASE, *sys.argv[2:]]
argv = sys.argv[1:]


class OverrideLoader(yaml.SafeLoader):
    pass


OverrideLoader.add_constructor("!reset", lambda loader, node: None)


def command() -> str:
    index = argv.index("--env-file") + 2 if "--env-file" in argv else 0
    if index and argv[index] == "--profile":
        index += 2
    return argv[index] if index else "version"


def files() -> list[Path]:
    return [Path(argv[i + 1]) for i, item in enumerate(argv) if item == "--file"]


def references(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return references(value.get("path"))
    if isinstance(value, list):
        return [name for item in value for name in references(item)]
    return []


def parse(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip().removeprefix("export").strip()
        key, separator, value = text.partition("=")
        if not separator or text.startswith("#"):
            continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        found[key.strip()] = value
    return found


def rewrite(out: dict[str, Any], data: dict[str, Any], paths: list[Path]) -> dict[str, Any]:
    directory = paths[0].parent
    override: dict[str, Any] = {}
    if len(paths) > 1:
        override = yaml.load(paths[1].read_text(encoding="utf-8"), Loader=OverrideLoader) or {}
    for key, service in (data.get("services") or {}).items():
        shown = out["services"].get(key)
        if shown is None:
            continue
        extended = (service or {}).get("extends")
        if isinstance(extended, dict) and extended.get("file"):
            document = yaml.safe_load((directory / Path(extended["file"]).name).read_text())
            for field, value in document["services"][extended["service"]].items():
                shown.setdefault(field, value)
        shown.pop("extends", None)
        for field in ("env_file", "label_file"):
            names = [Path(name).name for name in references((service or {}).get(field))]
            shown.pop(field, None)
            if not names:
                continue
            merged: dict[str, str] = {}
            for name in names:
                merged.update(parse(directory / name))
            target = "environment" if field == "env_file" else "labels"
            current = shown.get(target)
            shown[target] = {**(current if isinstance(current, dict) else {}), **merged}
            if field == "label_file":
                foreign = (data.get("x-fake") or {}).get("label_path")
                shown[field] = [foreign or str(directory / name) for name in names]
    for section in ("configs", "secrets"):
        declared = data.get(section) or {}
        for key, entry in declared.items():
            shown_entry: dict[str, Any] = {"name": f"{out['name']}_{key}"}
            shown_entry.update(entry or {})
            if "file" in shown_entry:
                variable = ((override.get(section) or {}).get(key) or {}).get("environment")
                if variable:
                    del shown_entry["file"]
                    shown_entry["environment"] = variable
                else:
                    shown_entry["file"] = str(directory / Path(shown_entry["file"]).name)
            out.setdefault(section, {})[key] = shown_entry
    return out


def record(cmd: str, paths: list[Path]) -> None:
    main = paths[0]
    try:
        script = (yaml.safe_load(main.read_text(encoding="utf-8")) or {}).get("x-fake") or {}
    except yaml.YAMLError:
        return
    if not script.get("record"):
        return
    held = {}
    for member in sorted(main.parent.iterdir()):
        if member.is_file() and not member.is_symlink():
            held[member.name] = {
                "mode": oct(member.stat().st_mode & 0o777),
                "content": member.read_text(encoding="utf-8"),
            }
    override = paths[1].read_text(encoding="utf-8") if len(paths) > 1 else None
    entry = {"cmd": cmd, "argv": argv, "override": override, "directory": held}
    with Path(f"{script['record']}.files").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def main() -> int:
    cmd = command()
    paths = files()
    if cmd != "version" and paths:
        record(cmd, paths)
    buffer = io.StringIO()
    code: Any = 0
    with contextlib.redirect_stdout(buffer):
        try:
            runpy.run_path(BASE, run_name="__main__")
        except SystemExit as stop:
            code = stop.code
    printed = buffer.getvalue()
    if cmd == "config" and not code:
        data = yaml.safe_load(paths[0].read_text(encoding="utf-8")) or {}
        interpolated = json.loads(printed)
        printed = json.dumps(rewrite(interpolated, data, paths)) + "\n"
    sys.stdout.write(printed)
    return int(code or 0)


sys.exit(main())
