"""A fake Swarm manager serving services, tasks, nodes, configs and secrets."""

import asyncio
import base64
import calendar
import copy
import json
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Reply, json_reply, split_version

MANAGER = "n" + "0" * 23 + "1"
WORKER = "n" + "0" * 23 + "2"
DIGEST = "sha256:" + "d" * 64
MULTIPLEXED = "application/vnd.docker.multiplexed-stream"
RAW = "application/vnd.docker.raw-stream"
STDOUT, STDERR = 1, 2
LogEntry = tuple[str, str, str]
SWARM_HEADS = frozenset({"services", "tasks", "nodes", "configs", "secrets", "distribution"})


def swarm_id(prefix: str, number: int) -> str:
    """Return a 25-character Swarm id starting with the prefix."""
    return f"{prefix}{number:0{25 - len(prefix)}d}"


def stamp(number: int) -> str:
    """Return an RFC 3339 timestamp, one second per number."""
    return f"2026-01-01T00:{number // 60 % 60:02d}:{number % 60:02d}.000000000Z"


@dataclass(frozen=True, slots=True)
class ServicePort:
    """A port a service publishes: the service spec asks for it and the endpoint reports it."""

    published: int
    mode: str = "ingress"
    protocol: str = "tcp"
    target: int = 80


def plain_spec(
    name: str, image: str = "busybox:latest", replicas: int = 1, **extra: Any
) -> dict[str, Any]:
    """Return the smallest replicated ServiceSpec; `extra` keys land beside TaskTemplate."""
    return {
        "Name": name,
        "Mode": {"Replicated": {"Replicas": replicas}},
        "TaskTemplate": {"ContainerSpec": {"Image": image}},
        **extra,
    }


class SwarmEngine(ContainerEngine):
    """Serves the Swarm endpoints from in-memory tables; a test adds what it needs."""

    def __init__(self) -> None:
        super().__init__()
        self.services: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.configs: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.logs: dict[str, list[LogEntry]] = {}
        self.auth_headers: list[tuple[str, str | None]] = []
        self.registry_auth_from: list[str | None] = []
        self.create_bodies: list[tuple[str, dict[str, Any]]] = []
        self.digest = DIGEST
        self.digests: dict[str, str] = {}
        self.distribution_fails = False
        self.stale_next = False
        self.stale_moves_version = False
        self.secret_data_visible = False
        self.create_status: int | None = None
        self.remove_status: int | None = None
        self.outage: set[str] = set()
        self.lingering_networks: dict[str, dict[str, Any]] = {}
        self.gate: asyncio.Event | None = None
        self.arrived = asyncio.Event()
        self._counter = 0
        for position, (node_id, hostname, role) in enumerate(
            ((MANAGER, "mgr", "manager"), (WORKER, "wrk", "worker"))
        ):
            self.nodes[node_id] = _node(node_id, hostname, role, position)

    def linger_network(self, network_id: str) -> None:
        """Remove a network but keep listing it; inspecting it answers 404."""
        self.lingering_networks[network_id] = self.networks.pop(network_id)

    async def _networks(self, method: str, parts: list[str], body: bytes) -> Reply | None:
        if method == "GET" and parts == ["networks"]:
            listed = [*self.networks.values(), *self.lingering_networks.values()]
            return json_reply([self._network_listing(n) for n in listed])
        if method == "GET" and len(parts) == 2 and parts[1] in self.lingering_networks:
            return json_reply({"message": "network not found"}, 404)
        return await super()._networks(method, parts, body)

    def release_gates(self) -> None:
        super().release_gates()
        if self.gate is not None:
            self.gate.set()

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def add_service(self, spec: dict[str, Any]) -> str:
        """Add a service holding this spec, place its tasks, and return its id."""
        number = self._next()
        service_id = swarm_id("s", number)
        self.services[service_id] = {
            "ID": service_id,
            "Version": {"Index": 10 + number},
            "CreatedAt": stamp(number),
            "UpdatedAt": stamp(number),
            "Spec": copy.deepcopy(spec),
        }
        self._place(service_id)
        return service_id

    def named_service(
        self,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        configs: list[tuple[str, str]] = (),  # type: ignore[assignment]
        secrets: list[tuple[str, str]] = (),  # type: ignore[assignment]
        networks: list[str] = (),  # type: ignore[assignment]
        vips: list[str] = (),  # type: ignore[assignment]
    ) -> str:
        """Add a one-replica service referencing the given (id, name) configs and secrets."""
        spec = plain_spec(name)
        if labels:
            spec["Labels"] = dict(labels)
        container = spec["TaskTemplate"]["ContainerSpec"]
        if configs:
            container["Configs"] = [
                {"ConfigID": i, "ConfigName": n, "File": {"Name": f"/{n}"}} for i, n in configs
            ]
        if secrets:
            container["Secrets"] = [
                {"SecretID": i, "SecretName": n, "File": {"Name": n}} for i, n in secrets
            ]
        if networks:
            spec["TaskTemplate"]["Networks"] = [{"Target": t} for t in networks]
        service_id = self.add_service(spec)
        self.services[service_id]["Endpoint"] = {
            "VirtualIPs": [{"NetworkID": v, "Addr": "10.0.0.2/24"} for v in vips]
        }
        return service_id

    def published_service(
        self, name: str, ports: Sequence[ServicePort], *, labels: dict[str, str] | None = None
    ) -> str:
        """Add a one-replica service whose endpoint reports these published ports."""
        entries = [
            {
                "Protocol": p.protocol,
                "TargetPort": p.target,
                "PublishedPort": p.published,
                "PublishMode": p.mode,
            }
            for p in ports
        ]
        spec = plain_spec(name, EndpointSpec={"Ports": entries})
        if labels:
            spec["Labels"] = dict(labels)
        service_id = self.add_service(spec)
        self.services[service_id]["Endpoint"] = {"Ports": entries, "VirtualIPs": []}
        return service_id

    def add_config(
        self, name: str, data: bytes = b"hello", labels: dict[str, str] | None = None
    ) -> str:
        number = self._next()
        config_id = swarm_id("c", number)
        self.configs[config_id] = _object(config_id, number, name, labels, data)
        return config_id

    def add_secret(
        self, name: str, labels: dict[str, str] | None = None, *, data: bytes | None = None
    ) -> str:
        """Add a secret; `data` is held by the daemon but only returned when it is made visible."""
        number = self._next()
        secret_id = swarm_id("x", number)
        self.secrets[secret_id] = _object(secret_id, number, name, labels, data)
        return secret_id

    def add_log(self, task_id: str, ts: str, text: str, stream: str = "out") -> None:
        self.logs.setdefault(task_id, []).append((ts, text, stream))

    def service_tasks(self, service_id: str) -> list[str]:
        return [t for t, v in self.tasks.items() if v["ServiceID"] == service_id]

    def mutating(self) -> list[str]:
        """Return `METHOD path` for every non-GET request the daemon received."""
        return [f"{r.method} {r.path}" for r in self.requests if r.method != "GET"]

    def _place(self, service_id: str) -> None:
        service = self.services[service_id]
        spec = service["Spec"]
        for task in self.tasks.values():
            if task["ServiceID"] == service_id and task["DesiredState"] == "running":
                task["DesiredState"] = "shutdown"
                task["Status"]["State"] = "shutdown"
                task["Version"]["Index"] += 1
        active = [n for n, node in self.nodes.items() if node["Spec"]["Availability"] == "active"]
        for constraint in spec.get("TaskTemplate", {}).get("Placement", {}).get("Constraints", []):
            squeezed = constraint.replace(" ", "")
            if squeezed.startswith("node.id=="):
                active = [n for n in active if n == squeezed.split("==", 1)[1]]
        mode = spec.get("Mode", {})
        slots: list[tuple[int | None, str | None]] = []
        if "Replicated" in mode:
            for number in range(1, mode["Replicated"].get("Replicas", 1) + 1):
                slots.append((number, active[(number - 1) % len(active)] if active else None))
        elif "Global" in mode:
            slots = [(None, node) for node in active]
        for slot, node in slots:
            self._add_task(service_id, slot, node)

    def _add_task(self, service_id: str, slot: int | None, node: str | None) -> str:
        number = self._next()
        task_id = swarm_id("t", number)
        status: dict[str, Any] = {
            "State": "running" if node else "pending",
            "Message": "started" if node else "pending task scheduling",
        }
        if node:
            status["ContainerStatus"] = {"ContainerID": f"{number:064x}", "ExitCode": 0}
        else:
            status["Err"] = "no suitable node (scheduling constraints not satisfied on 1 node)"
        self.tasks[task_id] = {
            "ID": task_id,
            "Version": {"Index": 100 + number},
            "CreatedAt": stamp(number),
            "UpdatedAt": stamp(number),
            "ServiceID": service_id,
            **({"NodeID": node} if node else {}),
            **({"Slot": slot} if slot is not None else {}),
            "DesiredState": "running",
            "Status": status,
        }
        self.services[service_id].setdefault("log_tasks", []).append(task_id)
        return task_id

    async def _reply(self, scope: Any, body: bytes) -> Reply | None:
        _, path = split_version(scope["path"])
        method = scope["method"]
        query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        parts = path.strip("/").split("/")
        head = parts[0]
        if head in SWARM_HEADS and (method, path) in self._routes:
            return None
        if path == "/info" and method == "GET":
            return json_reply(_info())
        if head in self.outage:
            return json_reply({"message": "This node is not a swarm manager."}, 503)
        if path == "/swarm" and method == "GET":
            tokens = {"Worker": "SWMTKN-worker", "Manager": "SWMTKN-manager"}
            return json_reply(
                {"ID": "w" * 25, "CreatedAt": stamp(1), "UpdatedAt": stamp(1), "JoinTokens": tokens}
            )
        if head == "distribution":
            self.auth_headers.append(("distribution", headers.get("x-registry-auth")))
            if self.distribution_fails:
                return json_reply({"message": "no route"}, 500)
            reference = "/".join(parts[1:-1])
            return json_reply({"Descriptor": {"digest": self.digests.get(reference, self.digest)}})
        held = head in ("services", "configs", "secrets", "nodes") and method != "GET"
        if held and self.gate is not None:
            self.arrived.set()
            await self.gate.wait()
        if head == "services":
            return self._services(method, parts, query, body, headers)
        if head == "tasks":
            return self._tasks(parts, query)
        if head == "nodes":
            return self._nodes(method, parts, query, body)
        if head == "configs":
            return self._objects(self.configs, method, parts, body, config=True)
        if head == "secrets":
            return self._objects(self.secrets, method, parts, body, config=False)
        return await super()._reply(scope, body)

    def _resolve(self, table: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
        if key in table:
            return table[key]
        for item in table.values():
            if item.get("Spec", {}).get("Name") == key:
                return item
        matches = [v for k, v in table.items() if k.startswith(key)]
        return matches[0] if len(matches) == 1 else None

    def _view(self, service: dict[str, Any], status: bool) -> dict[str, Any]:
        out = {k: v for k, v in service.items() if k != "log_tasks"}
        out.setdefault("Endpoint", {"Ports": [], "VirtualIPs": []})
        if status:
            mine = [t for t in self.tasks.values() if t["ServiceID"] == service["ID"]]
            desired = [t for t in mine if t["DesiredState"] == "running"]
            running = [t for t in desired if t["Status"]["State"] == "running"]
            out["ServiceStatus"] = {"RunningTasks": len(running), "DesiredTasks": len(desired)}
        else:
            out["ServiceStatus"] = None
        return out

    def _services(
        self,
        method: str,
        parts: list[str],
        query: dict[str, str],
        body: bytes,
        headers: dict[str, str],
    ) -> Reply:
        if method == "GET" and len(parts) == 1:
            found = list(self.services.values())
            for prefix in json.loads(query.get("filters", "{}")).get("id", []):
                found = [s for s in found if s["ID"].startswith(prefix)]
            for label in json.loads(query.get("filters", "{}")).get("label", []):
                key, _, value = label.partition("=")
                found = [
                    s
                    for s in found
                    if key in s["Spec"].get("Labels", {})
                    and (not value or s["Spec"]["Labels"][key] == value)
                ]
            return json_reply([self._view(s, query.get("status") == "true") for s in found])
        if method == "POST" and parts[1:] == ["create"]:
            return self._service_create(body, headers)
        service = self._resolve(self.services, parts[1])
        if service is None:
            return json_reply({"message": "service not found"}, 404)
        if method == "GET" and len(parts) == 2:
            return json_reply(self._view(service, False))
        if method == "DELETE" and len(parts) == 2:
            if self.remove_status is not None:
                return json_reply({"message": "engine detail"}, self.remove_status)
            del self.services[service["ID"]]
            for task in self.tasks.values():
                if task["ServiceID"] == service["ID"]:
                    task["DesiredState"] = "shutdown"
            return Reply(200, b"")
        if method == "POST" and parts[2:] == ["update"]:
            return self._service_update(service, query, body, headers)
        if method == "GET" and parts[2:] == ["logs"]:
            return self._service_logs(service, query)
        return json_reply({"message": "no"}, 404)

    def _service_create(self, body: bytes, headers: dict[str, str]) -> Reply:
        spec = json.loads(body)
        self.create_bodies.append(("services", spec))
        self.auth_headers.append(("create", headers.get("x-registry-auth")))
        if self.create_status is not None:
            return json_reply({"message": "engine detail"}, self.create_status)
        if any(s["Spec"]["Name"] == spec["Name"] for s in self.services.values()):
            return json_reply({"message": "name conflict"}, 409)
        container = spec.get("TaskTemplate", {}).get("ContainerSpec", {})
        for field, table, key in (
            ("Secrets", self.secrets, "Secret"),
            ("Configs", self.configs, "Config"),
        ):
            for ref in container.get(field, []):
                found = table.get(ref.get(f"{key}ID", ""))
                if found is None or found["Spec"]["Name"] != ref.get(f"{key}Name"):
                    return json_reply({"message": f"{key.lower()} not found: x"}, 400)
        return json_reply({"ID": self.add_service(spec), "Warnings": None}, 201)

    def _service_update(
        self, service: dict[str, Any], query: dict[str, str], body: bytes, headers: dict[str, str]
    ) -> Reply:
        self.auth_headers.append(("update", headers.get("x-registry-auth")))
        self.registry_auth_from.append(query.get("registryAuthFrom"))
        if self.stale_next or int(query["version"]) != service["Version"]["Index"]:
            if self.stale_next and self.stale_moves_version:
                service["Version"]["Index"] += 1
            self.stale_next = False
            return json_reply(
                {"message": "rpc error: code = Unknown desc = update out of sequence"}, 500
            )
        new = json.loads(body)
        rolling_back = query.get("rollback") == "previous"
        if rolling_back:
            new = copy.deepcopy(service["PreviousSpec"])
        unchanged = not rolling_back and new == service["Spec"]
        service["PreviousSpec"] = service["Spec"]
        service["Spec"] = new
        service["Version"]["Index"] += 1
        if rolling_back:
            service["UpdateStatus"] = {
                "State": "rollback_completed",
                "Message": "rollback completed",
            }
        if not unchanged:
            self._place(service["ID"])
        return json_reply({"Warnings": None})

    def _service_logs(self, service: dict[str, Any], query: dict[str, str]) -> Reply:
        tty = bool(service["Spec"]["TaskTemplate"]["ContainerSpec"].get("TTY"))
        since = _since(query.get("since"))
        out, err = query.get("stdout") != "false", query.get("stderr") != "false"
        tail = query.get("tail")
        payload = b""
        for task_id in service.get("log_tasks", []):
            task = self.tasks[task_id]
            entries = [
                e
                for e in self.logs.get(task_id, [])
                if _nanoseconds(e[0]) >= since
                and ((e[2] == "out" and out) or (e[2] == "err" and err))
            ]
            if tail not in (None, "all"):
                entries = entries[-int(tail) :] if int(tail) else []
            token = (
                f"com.docker.swarm.node.id={task.get('NodeID', '')},"
                f"com.docker.swarm.service.id={service['ID']},com.docker.swarm.task.id={task_id}"
            )
            for ts, text, stream in entries:
                if tty:
                    payload += f"{ts} {token} {text}\r\n".encode()
                else:
                    line = f"{ts} {token} {text}\n".encode()
                    kind = STDOUT if stream == "out" else STDERR
                    payload += struct.pack(">BxxxI", kind, len(line)) + line
        return Reply(200, payload, {"Content-Type": RAW if tty else MULTIPLEXED})

    def _tasks(self, parts: list[str], query: dict[str, str]) -> Reply:
        if len(parts) == 1:
            filters = json.loads(query.get("filters", "{}"))
            found = list(self.tasks.values())
            if "service" in filters:
                services = [self._resolve(self.services, v) for v in filters["service"]]
                if any(s is None for s in services):
                    return json_reply({"message": "service not found"}, 404)
                ids = {s["ID"] for s in services if s}
                found = [t for t in found if t["ServiceID"] in ids]
            if "node" in filters:
                found = [t for t in found if t.get("NodeID") in filters["node"]]
            return json_reply(found)
        task = self.tasks.get(parts[1])
        return json_reply(task) if task else json_reply({"message": "no task"}, 404)

    def _nodes(self, method: str, parts: list[str], query: dict[str, str], body: bytes) -> Reply:
        if len(parts) == 1:
            return json_reply(list(self.nodes.values()))
        node = self._resolve(self.nodes, parts[1])
        if node is None:
            return json_reply({"message": "no node"}, 404)
        if method == "GET":
            return json_reply(node)
        if parts[2:] != ["update"]:
            return json_reply({"message": "no"}, 404)
        if self.stale_next or int(query["version"]) != node["Version"]["Index"]:
            if self.stale_next and self.stale_moves_version:
                node["Version"]["Index"] += 1
            self.stale_next = False
            return json_reply({"message": "update out of sequence"}, 500)
        node["Spec"] = json.loads(body)
        node["Version"]["Index"] += 1
        if node["Spec"]["Availability"] == "drain":
            self._drain(node["ID"])
        return Reply(200, b"")

    def _drain(self, node_id: str) -> None:
        others = [
            n
            for n, v in self.nodes.items()
            if n != node_id and v["Spec"]["Availability"] == "active"
        ]
        for task in list(self.tasks.values()):
            if task.get("NodeID") == node_id and task["DesiredState"] == "running":
                task["DesiredState"] = "shutdown"
                task["Status"]["State"] = "shutdown"
                self._add_task(task["ServiceID"], task.get("Slot"), others[0] if others else None)

    def _objects(
        self,
        table: dict[str, dict[str, Any]],
        method: str,
        parts: list[str],
        body: bytes,
        *,
        config: bool,
    ) -> Reply:
        collection = "configs" if config else "secrets"
        if len(parts) == 1 and method == "GET":
            return json_reply([self._shown(o, config) for o in table.values()])
        if parts[1:] == ["create"]:
            spec = json.loads(body)
            self.create_bodies.append((collection, spec))
            if self.create_status is not None:
                return json_reply({"message": "engine detail"}, self.create_status)
            if any(o["Spec"]["Name"] == spec["Name"] for o in table.values()):
                return json_reply({"message": "already exists"}, 409)
            number = self._next()
            object_id = swarm_id("c" if config else "x", number)
            table[object_id] = _object(
                object_id, number, spec["Name"], spec.get("Labels"), base64.b64decode(spec["Data"])
            )
            return json_reply({"ID": object_id}, 201)
        found = self._resolve(table, parts[1])
        if found is None:
            return json_reply({"message": "not found"}, 404)
        if method == "GET":
            return json_reply(self._shown(found, config))
        if self.remove_status is not None:
            return json_reply({"message": "engine detail"}, self.remove_status)
        field, key = ("Configs", "ConfigID") if config else ("Secrets", "SecretID")
        for service in self.services.values():
            container = service["Spec"]["TaskTemplate"]["ContainerSpec"]
            if any(ref.get(key) == found["ID"] for ref in container.get(field, [])):
                return json_reply({"message": "is in use by the following service: x"}, 400)
        del table[found["ID"]]
        return Reply(200, b"")

    def _shown(self, item: dict[str, Any], config: bool) -> dict[str, Any]:
        shown = copy.deepcopy(item)
        if not config and not self.secret_data_visible:
            shown["Spec"].pop("Data", None)
        return shown


def _object(
    object_id: str, number: int, name: str, labels: dict[str, str] | None, data: bytes | None
) -> dict[str, Any]:
    spec: dict[str, Any] = {"Name": name, "Labels": dict(labels or {})}
    if data is not None:
        spec["Data"] = base64.b64encode(data).decode()
    return {
        "ID": object_id,
        "Version": {"Index": number},
        "CreatedAt": stamp(number),
        "UpdatedAt": stamp(number),
        "Spec": spec,
    }


def _node(node_id: str, hostname: str, role: str, position: int) -> dict[str, Any]:
    node: dict[str, Any] = {
        "ID": node_id,
        "Version": {"Index": 5 + position},
        "Spec": {"Labels": {}, "Role": role, "Availability": "active"},
        "Description": {
            "Hostname": hostname,
            "Platform": {"Architecture": "x86_64", "OS": "linux"},
            "Resources": {"NanoCPUs": 2_000_000_000, "MemoryBytes": 2**31},
            "Engine": {"EngineVersion": "27.5.1"},
        },
        "Status": {"State": "ready", "Addr": f"10.0.0.{position + 1}"},
    }
    if role == "manager":
        node["ManagerStatus"] = {
            "Leader": True,
            "Reachability": "reachable",
            "Addr": "10.0.0.1:2377",
        }
    return node


def _info() -> dict[str, Any]:
    return {
        "ID": "daemon",
        "Swarm": {
            "NodeID": MANAGER,
            "LocalNodeState": "active",
            "ControlAvailable": True,
            "Nodes": 2,
            "Managers": 1,
            "NodeAddr": "10.0.0.1",
        },
        "DefaultRuntime": "runc",
        "LoggingDriver": "json-file",
        "CgroupVersion": "2",
    }


def _nanoseconds(text: str) -> int:
    whole, _, fraction = text.rstrip("Z").partition(".")
    seconds = calendar.timegm(time.strptime(whole, "%Y-%m-%dT%H:%M:%S"))
    return seconds * 1_000_000_000 + int(fraction.ljust(9, "0")[:9] or 0)


def _since(text: str | None) -> int:
    if not text:
        return 0
    seconds, _, rest = text.partition(".")
    return int(seconds) * 1_000_000_000 + int(rest.ljust(9, "0")[:9] or 0)
