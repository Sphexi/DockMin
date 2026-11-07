"""A scripted Compose binary for the stack tests.

The top-level `x-fake` key of the compose file scripts `up: ok|fail|hang|slow|warn`,
`down: ok|fail|slow|warn`, `start`, `stop` and `config: fail`, and `record: <path>`;
`config --no-interpolate` merges every `--file` and prints the YAML.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml

argv = sys.argv[1:]


def after(flag: str) -> str:
    return argv[argv.index(flag) + 1]


index = argv.index("--env-file") + 2 if "--env-file" in argv else 0
if index and argv[index] == "--profile":
    index += 2
cmd = argv[index] if index else "version"
sub = argv[index + 1] if len(argv) > index + 1 else ""


def say(obj: object) -> None:
    print(json.dumps(obj), file=sys.stderr, flush=True)


def event(resource: str, status: str, text: str) -> None:
    say({"id": resource, "status": status, "text": text})


def fail(message: str, code: int = 1) -> None:
    say({"error": True, "message": message})
    sys.exit(code)


if cmd == "version":
    print(json.dumps({"version": os.environ.get("FAKE_VERSION", "v2.33.0")}))
    sys.exit(0)


def merged(into: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    for key, value in other.items():
        held = into.get(key)
        if isinstance(held, dict) and isinstance(value, dict):
            merged(held, value)
        elif isinstance(held, list) and isinstance(value, list):
            held.extend(value)
        else:
            into[key] = value
    return into


if cmd == "config" and "--no-interpolate" in argv:
    combined: dict[str, Any] = {}
    for position, flag in enumerate(argv):
        if flag == "--file":
            merged(combined, yaml.safe_load(Path(argv[position + 1]).read_text(encoding="utf-8")))
    print(yaml.safe_dump({"name": after("--project-name"), **combined}, sort_keys=False))
    sys.exit(0)

file = Path(after("--file"))
raw = file.read_text(encoding="utf-8")
project = after("--project-name")
pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)([^}]*))?\}")


def replace(match: re.Match[str]) -> str:
    name, operator, default = match.group(1), match.group(2), match.group(3)
    if name in os.environ:
        return os.environ[name]
    if operator:
        return default or ""
    return ""


text = pattern.sub(replace, raw.replace("$$", "\0"))
for name in set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\?", text)):
    if name not in os.environ:
        fail(f"required variable {name} is missing a value")
text = text.replace("\0", "$")
try:
    data = yaml.safe_load(text) or {}
except yaml.YAMLError:
    fail("yaml: invalid")


class Reset:
    """The value of a `!reset` tag: the key it is set on is dropped from the merged service."""


class OverrideLoader(yaml.SafeLoader):
    pass


OverrideLoader.add_constructor("!reset", lambda loader, node: Reset())


def override_services(into: dict[str, Any]) -> None:
    """Apply the `services` of every further `--file`, as Compose merges them."""
    files = [argv[position + 1] for position, flag in enumerate(argv) if flag == "--file"][1:]
    for name in files:
        extra = yaml.load(Path(name).read_text(encoding="utf-8"), Loader=OverrideLoader) or {}
        for key, service in (extra.get("services") or {}).items():
            held = into.setdefault("services", {}).setdefault(key, {})
            for field, value in (service or {}).items():
                if isinstance(value, Reset):
                    held.pop(field, None)
                else:
                    held[field] = value


override_services(data)
script = data.get("x-fake") or {}
if script.get("record"):
    cert = os.environ.get("DOCKER_CERT_PATH")
    listing = {}
    if cert and Path(cert).is_dir():
        for member in sorted(Path(cert).iterdir()):
            listing[member.name] = oct(member.stat().st_mode & 0o777)
    with Path(script["record"]).open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "cmd": cmd,
                    "argv": argv,
                    "env": dict(os.environ),
                    "cwd": str(Path.cwd()),
                    "cert": listing,
                    "dir_files": sorted(member.name for member in file.parent.iterdir()),
                }
            )
            + "\n"
        )

services = data.get("services") or {}
if cmd == "config" and script.get("config") == "fail":
    fail("services.web.image is invalid", 1)
if cmd == "config":
    out: dict[str, Any] = {"name": project, "services": {}, "networks": {}, "volumes": {}}
    uses_default = False
    for key, svc in services.items():
        svc = dict(svc or {})
        env = svc.get("environment")
        if isinstance(env, list):
            svc["environment"] = dict(item.split("=", 1) for item in env if "=" in item)
        ports = svc.get("ports")
        if isinstance(ports, list):
            longs = []
            for port in ports:
                if isinstance(port, dict):
                    longs.append(port)
                    continue
                host, _, target = str(port).rpartition(":")
                longs.append(
                    {
                        "mode": "ingress",
                        "target": int(target.split("/")[0]),
                        "published": host or None,
                        "protocol": "tcp",
                    }
                )
            svc["ports"] = longs
        mounts = svc.get("volumes")
        if isinstance(mounts, list):
            longs = []
            for mount in mounts:
                if isinstance(mount, dict):
                    longs.append(mount)
                    continue
                parts = str(mount).split(":")
                source, target = (parts[0], parts[1]) if len(parts) > 1 else (None, parts[0])
                ro = len(parts) > 2 and parts[2] == "ro"
                entry: dict[str, Any]
                if source and source.startswith(("/", ".")):
                    entry = {"type": "bind", "source": source, "target": target, "bind": {}}
                else:
                    entry = {"type": "volume", "target": target, "volume": {}}
                    if source:
                        entry["source"] = source
                if ro:
                    entry["read_only"] = True
                longs.append(entry)
            svc["volumes"] = longs
        build = svc.get("build")
        if build is not None:
            body = {"context": build} if isinstance(build, str) else dict(build)
            body["context"] = os.path.normpath(Path(after("--project-directory")) / body["context"])
            body.setdefault("dockerfile", "Dockerfile")
            svc["build"] = body
        nets = svc.get("networks")
        if isinstance(nets, list):
            svc["networks"] = dict.fromkeys(nets)
        elif not isinstance(nets, dict):
            svc["networks"] = {"default": None}
        if "default" in svc["networks"]:
            uses_default = True
        out["services"][key] = svc
    for section in ("volumes", "networks"):
        for key, item in (data.get(section) or {}).items():
            item = dict(item or {})
            if item.get("external"):
                item.setdefault("name", key)
            else:
                item.setdefault("name", f"{project}_{key}")
            out[section][key] = item
    if uses_default and "default" not in out["networks"]:
        out["networks"]["default"] = {"name": f"{project}_default"}
    if "x-fake" in data:
        out["x-fake"] = data["x-fake"]
    print(json.dumps(out))
    sys.exit(0)

if cmd == "ps":
    sys.exit(0)

mode = script.get("down" if cmd == "down" else "up", "ok")
if cmd in {"up", "start", "stop"} and script.get("up") == "hang":
    event(f"Container {project}-web-1", "Working", "Starting")
    time.sleep(300)
if cmd == "up" and mode == "fail":
    event(f"Container {project}-web-1", "Working", "Starting")
    print(
        f"token=hunter2secretvalue in {file.parent} and SECRET_SENTINEL_VALUE here",
        file=sys.stderr,
    )
    fail("port is already allocated", 3)
if cmd == "start" and script.get("start") == "fail":
    fail("dependency failed to start: container x is unhealthy", 1)
if cmd == "stop" and script.get("stop") == "fail":
    fail("stop failed: container x did not exit", 1)
if cmd == "down" and mode == "fail":
    fail("down failed", 4)
if mode == "slow":
    time.sleep(0.8)
if mode == "warn":
    say({"level": "warning", "msg": "a warning", "time": "t"})
DONE_TEXT = {"up": "Started", "down": "Removed", "start": "Started", "stop": "Stopped"}
for key in services:
    resource = f"Container {project}-{key}-1"
    event(resource, "Working", "Working")
    event(resource, "Done", DONE_TEXT[cmd])
sys.exit(0)
