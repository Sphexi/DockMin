"""A Compose stand-in for Swarm stack tests: `config` prints the file as a resolved model.

Only `version` and `config` exist; any other command exits 9. Every argv is appended to `argv.log`
beside the script, so a test copies the script into its own directory first.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

argv = sys.argv[1:]
LOG = Path(__file__).with_name("argv.log")


def after(flag: str) -> str:
    return argv[argv.index(flag) + 1]


index = argv.index("--env-file") + 2 if "--env-file" in argv else 0
if index and argv[index] == "--profile":
    index += 2
cmd = argv[index] if index else "version"

with LOG.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"cmd": cmd, "argv": argv}) + "\n")
if cmd == "version":
    print(json.dumps({"version": os.environ.get("FAKE_VERSION", "v2.33.0")}))
    sys.exit(0)

file = Path(after("--file"))
project = after("--project-name")
pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)([^}]*))?\}")


def replace(match: re.Match[str]) -> str:
    name, operator, default = match.group(1), match.group(2), match.group(3)
    if name in os.environ:
        return os.environ[name]
    return (default or "") if operator else ""


text = pattern.sub(replace, file.read_text(encoding="utf-8").replace("$$", "\0")).replace("\0", "$")
try:
    data = yaml.safe_load(text) or {}
except yaml.YAMLError:
    print(json.dumps({"error": True, "message": "yaml: invalid"}), file=sys.stderr)
    sys.exit(1)

if cmd != "config":
    print(json.dumps({"error": True, "message": f"unexpected {cmd}"}), file=sys.stderr)
    sys.exit(9)

out: dict[str, Any] = {
    "name": project,
    "services": {},
    "networks": {},
    "configs": {},
    "secrets": {},
    "volumes": {},
}
used: dict[str, set[str]] = {"networks": set(), "configs": set(), "secrets": set()}
for key, raw in (data.get("services") or {}).items():
    svc = dict(raw or {})
    svc.setdefault("command", None)
    svc.setdefault("entrypoint", None)
    env = svc.get("environment")
    if isinstance(env, list):
        svc["environment"] = dict(item.split("=", 1) for item in env if "=" in item)
    if "deploy" in svc:
        deploy = dict(svc["deploy"] or {})
        deploy.setdefault("resources", {})
        deploy.setdefault("placement", {})
        svc["deploy"] = deploy
    ports = svc.get("ports")
    if isinstance(ports, list):
        longs = []
        for port in ports:
            if isinstance(port, dict):
                longs.append(port)
                continue
            host, _, target = str(port).rpartition(":")
            long_port = {"mode": "ingress", "target": int(target.split("/")[0]), "protocol": "tcp"}
            if host:
                long_port["published"] = host
            longs.append(long_port)
        svc["ports"] = longs
    nets = svc.get("networks")
    if isinstance(nets, list):
        svc["networks"] = dict.fromkeys(nets)
    elif not isinstance(nets, dict):
        svc["networks"] = {"default": None}
    used["networks"].update(svc["networks"])
    for group in ("configs", "secrets"):
        refs = []
        for ref in svc.get(group) or []:
            entry: dict[str, Any] = {"source": ref} if isinstance(ref, str) else dict(ref)
            if group == "secrets":
                entry.setdefault("target", f"/run/secrets/{entry['source']}")
            used[group].add(entry["source"])
            refs.append(entry)
        if refs:
            svc[group] = refs
    out["services"][key] = svc
for section in ("networks", "configs", "secrets"):
    declared = dict(data.get(section) or {})
    if section == "networks" and "default" in used["networks"]:
        declared.setdefault("default", {})
    for key in sorted(used[section]):
        item = dict(declared.get(key) or {})
        item.setdefault("name", key if item.get("external") else f"{project}_{key}")
        if section == "networks":
            item.setdefault("ipam", {})
        out[section][key] = item
for key, item in (data.get("volumes") or {}).items():
    item = dict(item or {})
    item.setdefault("name", key if item.get("external") else f"{project}_{key}")
    out["volumes"][key] = item
print(json.dumps(out))
