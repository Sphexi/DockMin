"""A fake Engine with container behaviour: listing, inspect, lifecycle, removal, create and pulls.

Resolves a path reference the way the daemon does (exact id, then exact name, then a unique id
prefix), so a test can reproduce a name that collides with another container's id. A container
made through the create endpoint, or added with `add_configured`, is inspected the way a daemon
reports one: image defaults merged in, daemon defaults filled in, mounts and endpoints resolved.
"""

import asyncio
import copy
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote

from app.docker_engine.resources import CONTAINER_LIST_BYTES
from tests.docker_engine.fake_engine import (
    FakeEngine,
    Message,
    Receive,
    Reply,
    Scope,
    Send,
    error_reply,
    json_reply,
    split_version,
)

ENGINE_TEXT = "engine detail other-container-7a1b"
CREATED = 1_700_000_000
LIVE_STATES = frozenset({"running", "paused", "restarting"})
ANONYMOUS_LABEL = "com.docker.volume.anonymous"
TRUTHY = frozenset({"1", "true", "True"})
IMAGE_ID = "sha256:" + "a" * 64
DAEMON_DIGEST = "sha256:" + "e" * 64
HUB_PREFIXES = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")
LIBRARY = "library/"
BRIDGE_ID = "b" * 64
HOST_NETWORK_ID = "c" * 64
NONE_NETWORK_ID = "d" * 64
MAC_ADDRESS = "02:42:ac:11:00:02"
NETWORK_CREATED = "2024-05-06T07:08:09.000000000Z"
VOLUME_CREATED = "2024-05-06T07:08:09Z"
CREATED_NETWORK_BASE = 0xD0000
PREDEFINED_NETWORKS = frozenset({"bridge", "host", "none"})
VOLUME_IN_USE = "volume is in use"
VOLUME_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")
IMAGE_ONLY_KEYS = ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck", "StopSignal", "Shell")
MASKED_PATHS = [
    "/proc/acpi",
    "/proc/kcore",
    "/proc/keys",
    "/proc/latency_stats",
    "/proc/timer_list",
    "/proc/sched_debug",
    "/proc/scsi",
    "/sys/firmware",
]
READONLY_PATHS = ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"]


def created_id(number: int) -> str:
    """Return the id the daemon gives the numbered container made through the create endpoint."""
    return f"{0xC0000 + number:064x}"


def created_network_id(number: int) -> str:
    """Return the id the daemon gives the numbered network made through the create endpoint."""
    return f"{CREATED_NETWORK_BASE + number:064x}"


def swarm_network_id(number: int) -> str:
    """Return a Swarm-scoped network id: 25 lower-case base-36 characters, not hexadecimal."""
    return f"z{number:024d}"


def anonymous_volume(number: int) -> str:
    """Return the name the daemon gives the numbered anonymous volume it creates."""
    return f"{0xAA0000 + number:064x}"


def engine_time(seconds: int) -> str:
    """Format a moment the way the Engine writes it in an inspect document."""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def familiar_name(text: str) -> str:
    """Write a Docker Hub reference the way the daemon prints it: no host, no `library/`."""
    for prefix in HUB_PREFIXES:
        if text.startswith(prefix):
            name = text[len(prefix) :]
            if name.startswith(LIBRARY) and "/" not in name[len(LIBRARY) :]:
                return name[len(LIBRARY) :]
            return name
    return text


def repository_of(reference: str) -> str:
    """Return a reference without its tag or digest."""
    name = reference.partition("@")[0]
    head, colon, tail = name.rpartition(":")
    return head if colon and "/" not in tail else name


def stats_line(step: int = 0) -> bytes:
    """Build one newline-terminated stats frame; every counter grows with `step`."""
    frame = {
        "read": engine_time(CREATED + step),
        "cpu_stats": {
            "cpu_usage": {"total_usage": 1000 * (step + 2)},
            "system_cpu_usage": 100_000 * (step + 2),
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1000 * (step + 1)},
            "system_cpu_usage": 100_000 * (step + 1),
            "online_cpus": 2,
        },
        "memory_stats": {
            "usage": 52_428_800 + step,
            "limit": 1_073_741_824,
            "stats": {"inactive_file": 1024},
        },
        "networks": {"eth0": {"rx_bytes": 1000 * step, "tx_bytes": 500 * step}},
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "read", "value": 4096},
                {"op": "write", "value": 8192},
            ]
        },
        "pids_stats": {"current": 4},
    }
    return json.dumps(frame).encode("utf-8") + b"\n"


@dataclass(slots=True)
class Gate:
    """Holds a lifecycle request inside the daemon until the test lets it finish."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class Publish:
    """A host port a container is configured to publish.

    The list endpoint reports it only while the container is running or paused; inspect always
    reports it under `HostConfig.PortBindings`.
    """

    host_port: int
    container_port: int = 80
    host_ip: str = ""
    protocol: str = "tcp"


@dataclass(slots=True)
class Box:
    """One container as the daemon holds it."""

    id: str
    name: str
    state: str = "running"
    labels: dict[str, str] = field(default_factory=dict)
    image: str = "nginx:1"
    health: str | None = None
    cmd: list[str] = field(default_factory=lambda: ["run"])
    entrypoint: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    mounts: list[dict[str, Any]] = field(default_factory=list)
    ports: list[dict[str, Any]] = field(default_factory=list)
    stop_timeout: int | None = None
    privileged: bool = False
    restart_count: int = 0
    exit_code: int = 0
    created_text: str | None = None
    image_id: str = "sha256:aaa"
    config: dict[str, Any] | None = None
    host_config: dict[str, Any] | None = None
    endpoints: dict[str, dict[str, Any]] | None = None
    publishes: list[Publish] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Return the row the list endpoint gives for this container."""
        return {
            "Id": self.id,
            "Names": [f"/{self.name}"],
            "Image": self.image,
            "ImageID": self.image_id,
            "Created": CREATED,
            "State": self.state,
            "Status": "Up 2 hours engine-status-text",
            "Labels": self.labels,
            "Ports": [*self.ports, *self._listed_publishes()],
            "Mounts": [
                {"Type": m["Type"], "Name": m.get("Name"), "Destination": m["Destination"]}
                for m in self.mounts
            ],
            "NetworkSettings": {
                "Networks": {
                    name: {"NetworkID": endpoint.get("NetworkID", "")}
                    for name, endpoint in self._reported_endpoints().items()
                }
            },
            "HostConfig": {"NetworkMode": self._host_config().get("NetworkMode", "default")},
        }

    def inspect(self) -> dict[str, Any]:
        """Return the document the inspect endpoint gives for this container."""
        live = self.state in LIVE_STATES
        state: dict[str, Any] = {
            "Status": self.state,
            "Running": self.state == "running",
            "Paused": self.state == "paused",
            "Restarting": self.state == "restarting",
            "ExitCode": 0 if live else self.exit_code,
            "StartedAt": engine_time(CREATED + 60),
            "FinishedAt": "0001-01-01T00:00:00Z" if live else engine_time(CREATED + 120),
        }
        if self.health is not None:
            state["Health"] = {
                "Status": self.health,
                "FailingStreak": 3 if self.health == "unhealthy" else 0,
            }
        published = {
            f"{port['PrivatePort']}/{port['Type']}": [
                {"HostIp": port.get("IP", ""), "HostPort": str(port["PublicPort"])}
            ]
            for port in self.ports
            if "PublicPort" in port
        }
        return {
            "Id": self.id,
            "Name": f"/{self.name}",
            "Created": self.created_text or engine_time(CREATED),
            "Image": self.image_id,
            "RestartCount": self.restart_count,
            "State": state,
            "Config": self._config(),
            "HostConfig": self._host_config(),
            "NetworkSettings": {"Networks": self._reported_endpoints(), "Ports": published},
            "Mounts": self.mounts,
        }

    def _config(self) -> dict[str, Any]:
        if self.config is not None:
            return self.config
        return {
            "Image": self.image,
            "Env": self.env,
            "Cmd": self.cmd,
            "Entrypoint": self.entrypoint,
            "Labels": self.labels,
            "StopTimeout": self.stop_timeout,
        }

    def _listed_publishes(self) -> list[dict[str, Any]]:
        if self.state not in ("running", "paused"):
            return []
        return [
            {
                "IP": p.host_ip,
                "PrivatePort": p.container_port,
                "PublicPort": p.host_port,
                "Type": p.protocol,
            }
            for p in self.publishes
        ]

    def _host_config(self) -> dict[str, Any]:
        if self.host_config is not None:
            return self.host_config
        config: dict[str, Any] = {
            "Privileged": self.privileged,
            "NetworkMode": "bridge",
            "RestartPolicy": {"Name": "always"},
        }
        if self.publishes:
            bindings: dict[str, list[dict[str, str]]] = {}
            for p in self.publishes:
                bindings.setdefault(f"{p.container_port}/{p.protocol}", []).append(
                    {"HostIp": p.host_ip, "HostPort": str(p.host_port)}
                )
            config["PortBindings"] = bindings
        return config

    def _reported_endpoints(self) -> dict[str, dict[str, Any]]:
        """Return the endpoints as the daemon reports them: ids stay empty until first start."""
        endpoints = self._endpoints()
        if self.state != "created":
            return endpoints
        return {name: {**endpoint, "NetworkID": ""} for name, endpoint in endpoints.items()}

    def _endpoints(self) -> dict[str, dict[str, Any]]:
        if self.endpoints is not None:
            return self.endpoints
        return {
            "bridge": {
                "IPAddress": "172.17.0.2",
                "Gateway": "172.17.0.1",
                "MacAddress": MAC_ADDRESS,
                "Aliases": [self.name],
            }
        }


@dataclass(slots=True)
class Failure:
    """An Engine error for a verb: every request, or only those for one container."""

    status: int
    target: str | None = None
    skip: int = 0
    times: int | None = None


class ContainerEngine(FakeEngine):
    """Serves container, volume, network, image and info endpoints; other routes are scripted."""

    def __init__(self) -> None:
        super().__init__()
        self.boxes: dict[str, Box] = {}
        self.volumes: dict[str, bool] = {}
        self.volume_docs: dict[str, tuple[str, dict[str, str]]] = {}
        self.volume_labels: dict[str, dict[str, str]] = {}
        self.volume_created: dict[str, str] = {}
        self.networks: dict[str, dict[str, Any]] = {}
        self.images: dict[str, dict[str, Any]] = {}
        self.image_store: dict[str, dict[str, Any]] = {}
        self.pull_id = IMAGE_ID
        self.pull_config: dict[str, Any] = {}
        self.pull_status: int | None = None
        self.pull_digest: str | None = None
        self.pull_lines: list[dict[str, Any]] = [{"status": "Pulling"}, {"status": "Done"}]
        self.pull_error: str | None = None
        self.push_status: int | None = None
        self.push_error: str | None = None
        self.push_digest: str | None = DAEMON_DIGEST
        self.push_lines: list[dict[str, Any]] = [{"status": "Preparing", "id": "aaaaaaaaaaaa"}]
        self.default_runtime = "runc"
        self.logging_driver = "json-file"
        self.cgroup_version = "2"
        self._gates: dict[str, Gate] = {}
        self._hooks: dict[str, Callable[[], Awaitable[None] | None]] = {}
        self._failures: dict[str, Failure] = {}
        self._created = 0
        self._anonymous = 0
        self._made_networks = 0
        self.health_listing_status: int | None = None
        self.listing_status: int | None = None
        self.oversize_listing = False
        self._default_routes = frozenset(self._routes)
        for name, network_id, driver in (
            ("bridge", BRIDGE_ID, "bridge"),
            ("host", HOST_NETWORK_ID, "host"),
            ("none", NONE_NETWORK_ID, "null"),
        ):
            self.networks[network_id] = _network_document(network_id, name, driver)

    def add(self, container_id: str, name: str, **fields: Any) -> Box:
        """Add a container to the daemon and return it."""
        box = Box(container_id, name, **fields)
        self.boxes[container_id] = box
        return box

    def add_image(
        self,
        reference: str | None,
        image_id: str = IMAGE_ID,
        config: dict[str, Any] | None = None,
        *,
        digests: tuple[str, ...] = (),
        size: int = 1000,
        created: int = CREATED,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Make the daemon hold an image, optionally under a reference, and return its document.

        A reference already held by another image moves to this one, as a tag does. None for the
        reference adds an image with no tag, which the list shows as dangling.
        """
        document = self.image_store.get(image_id)
        if document is None:
            document = {
                "Id": image_id,
                "RepoTags": [],
                "RepoDigests": [],
                "Config": {},
                "Size": size,
                "Created": engine_time(created),
                "Architecture": "amd64",
                "Os": "linux",
            }
            self.image_store[image_id] = document
        if config:
            document["Config"] = config
        if history is not None:
            document["History"] = history
        for digest in digests:
            if digest not in document["RepoDigests"]:
                document["RepoDigests"].append(digest)
        if reference is not None:
            self._attach(document, reference)
        return document

    def _attach(self, document: dict[str, Any], reference: str) -> None:
        if "@" in reference:
            if reference not in document["RepoDigests"]:
                document["RepoDigests"].append(reference)
            return
        previous = self.images.get(reference)
        if previous is not None and previous is not document:
            previous["RepoTags"].remove(reference)
        if reference not in document["RepoTags"]:
            document["RepoTags"].append(reference)
        self.images[reference] = document

    def remove_image(self, image_id: str) -> None:
        """Make the daemon forget an image, as `docker image rm` does."""
        self.image_store.pop(image_id, None)
        for reference in [r for r, d in self.images.items() if d["Id"] == image_id]:
            del self.images[reference]

    def tags_of(self, image_id: str) -> list[str]:
        """Return the references the daemon holds for an image, empty when it is gone."""
        document = self.image_store.get(image_id)
        return [] if document is None else list(document["RepoTags"])

    def add_network(
        self,
        name: str,
        number: int,
        driver: str = "bridge",
        *,
        network_id: str | None = None,
        **fields: Any,
    ) -> str:
        """Add a user-defined network and return its id.

        The id is built from the number unless one is given; `fields` override the document, for
        example Labels, IPAM, Options, Scope or Attachable.
        """
        network_id = network_id or f"{number:064x}"
        self.networks[network_id] = _network_document(network_id, name, driver, **fields)
        return network_id

    def add_volume(
        self,
        name: str,
        *,
        anonymous: bool = False,
        driver: str = "local",
        options: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        created: str | None = None,
    ) -> None:
        """Add a volume; options such as `o=bind` make it host-backed."""
        self.volumes[name] = anonymous
        self.volume_docs[name] = (driver, options or {})
        self.volume_labels[name] = dict(labels or {})
        if created is not None:
            self.volume_created[name] = created

    def network_bodies(self, verb: str) -> list[dict[str, Any]]:
        """Return the decoded body of every network `create`, `connect` or `disconnect` request."""
        return [
            json.loads(r.body)
            for r in self.requests
            if r.method == "POST" and r.path.rsplit("/", 1)[-1] == verb and "networks" in r.path
        ]

    def volume_removals(self) -> list[tuple[str, dict[str, str]]]:
        """Return the name and query of every volume removal request."""
        return [
            (r.path.rsplit("/", 1)[-1], dict(r.query))
            for r in self.requests
            if r.method == "DELETE" and r.path.startswith("/volumes/")
        ]

    def volume_bodies(self) -> list[dict[str, Any]]:
        """Return the decoded body of every volume create request."""
        return [json.loads(r.body) for r in self.calls("POST", "/volumes/create")]

    def add_configured(
        self, container_id: str, name: str, body: dict[str, Any], state: str = "running"
    ) -> Box:
        """Add a container as the daemon holds one created from this Engine create body."""
        made = self._materialise(container_id, name, body, state)
        if isinstance(made, Reply):
            raise AssertionError(made.body.decode())
        return made

    def hold(self, verb: str) -> Gate:
        """Make every request for the verb, or for "inspect", wait until its gate is released."""
        gate = Gate()
        self._gates[verb] = gate
        return gate

    def on(self, verb: str, hook: Callable[[], Awaitable[None] | None]) -> None:
        """Run the hook as the daemon receives each request for the verb, before it answers."""
        self._hooks[verb] = hook

    def clear_faults(self) -> None:
        """Forget every failure, hook, held gate and scripted route but the default ones."""
        self.release_gates()
        self._gates.clear()
        self._hooks.clear()
        self._failures.clear()
        self.pull_status = None
        self._routes = {k: v for k, v in self._routes.items() if k in self._default_routes}

    def release_gates(self) -> None:
        """Let every held request go, so a test that failed while holding one cannot hang."""
        for gate in self._gates.values():
            gate.release.set()

    def fail(
        self,
        verb: str,
        status: int,
        *,
        target: str | None = None,
        skip: int = 0,
        times: int | None = None,
    ) -> None:
        """Make requests for the verb answer with an Engine error of this status.

        target limits it to one container id; skip lets that many matching requests through
        first; times stops the failure after that many answers.
        """
        self._failures[verb] = Failure(status, target, skip, times)

    def mutations(self) -> list[str]:
        """Return `METHOD path` for every request that could change a container."""
        return [f"{r.method} {r.path}" for r in self.requests if r.method in ("POST", "DELETE")]

    def verbs(self) -> list[str]:
        """Return the verb of every mutating container or image request, in order."""
        found: list[str] = []
        for request in self.requests:
            if request.path == "/containers/create":
                found.append("create")
            elif request.path == "/images/create":
                found.append("pull")
            elif request.method == "POST" and request.path.startswith("/containers/"):
                found.append(request.path.rsplit("/", 1)[1])
            elif request.method == "DELETE" and request.path.startswith("/containers/"):
                found.append("remove")
        return found

    def bodies(self) -> list[dict[str, Any]]:
        """Return the decoded body of every container create request, in order."""
        return [json.loads(r.body) for r in self.calls("POST", "/containers/create")]

    def resolve(self, reference: str) -> Box | None:
        """Find a container by id, then by name, then by unique id prefix."""
        if reference in self.boxes:
            return self.boxes[reference]
        for box in self.boxes.values():
            if box.name == reference:
                return box
        matches = [box for box in self.boxes.values() if box.id.startswith(reference)]
        return matches[0] if len(matches) == 1 else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one request: dynamic routes first, scripted routes otherwise."""
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        reply = await self._reply(scope, body)
        if reply is None:
            await super().__call__(scope, receive, send)
            return
        headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in reply.headers.items()
        ]
        start: Message = {"type": "http.response.start", "status": reply.status, "headers": headers}
        await send(start)
        await send({"type": "http.response.body", "body": reply.body})

    async def _reply(self, scope: Scope, body: bytes) -> Reply | None:
        _, path = split_version(scope["path"])
        method = scope["method"]
        query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
        parts = path.strip("/").split("/")
        if parts[0] == "networks" and (method, path) not in self._routes:
            return await self._networks(method, parts, body)
        if parts[0] == "volumes" and (method, path) not in self._routes:
            return await self._volumes(method, parts, query, body)
        if path.startswith("/images/") and (method, path) in self._routes:
            return None
        if path == "/images/json" and method == "GET":
            return self._image_list()
        if parts[0] == "images" and parts[-1] == "json" and method == "GET":
            return self._image("/".join(parts[1:-1]))
        if path == "/images/create" and method == "POST":
            return await self._pull(scope, query)
        if parts[0] == "images" and parts[-1] == "history" and method == "GET":
            return self._image_history("/".join(parts[1:-1]))
        if parts[0] == "images" and parts[-1] == "tag" and method == "POST":
            return self._image_tag("/".join(parts[1:-1]), query)
        if parts[0] == "images" and parts[-1] == "push" and method == "POST":
            return await self._image_push("/".join(parts[1:-1]), query, scope)
        if parts[0] == "images" and len(parts) >= 2 and method == "DELETE":
            return self._image_delete("/".join(parts[1:]), query)
        if path == "/info" and method == "GET" and ("GET", "/info") not in self._routes:
            await self._pass_gate("info")
            return self._info()
        if parts[0] != "containers":
            return None
        if path == "/containers/json" and method == "GET":
            return self._list(query)
        if path == "/containers/create" and method == "POST":
            return await self._create(query, body)
        box = self.resolve(parts[1]) if len(parts) > 1 else None
        if box is None:
            return json_reply({"message": f"No such container: {ENGINE_TEXT}"}, 404)
        if method == "GET" and parts[2:] == ["json"]:
            await self._pass_gate("inspect")
            failed = self._failure("inspect", box.id)
            if failed is not None:
                return failed
            return json_reply(box.inspect())
        if method == "GET" and parts[2:] == ["stats"]:
            return Reply(200, stats_line().strip(), {"Content-Type": "application/json"})
        if method == "POST" and parts[2:] == ["rename"]:
            return self._rename(box, query)
        if method == "POST" and len(parts) == 3:
            return await self._act(box, parts[2])
        if method == "DELETE" and len(parts) == 2:
            return self._remove(box, query)
        return None

    def _list(self, query: dict[str, str]) -> Reply:
        if self.listing_status is not None:
            return json_reply({"message": ENGINE_TEXT}, self.listing_status)
        if self.oversize_listing:
            return Reply(200, b"[]", {"Content-Length": str(CONTAINER_LIST_BYTES + 1)})
        filters = json.loads(query.get("filters", "{}"))
        health = filters.get("health")
        if health is not None and self.health_listing_status is not None:
            return json_reply({"message": ENGINE_TEXT}, self.health_listing_status)
        everything = query.get("all", "false") in TRUTHY
        rows = [
            box.summary()
            for box in self.boxes.values()
            if (everything or box.state in LIVE_STATES or "status" in filters)
            and (health is None or box.health in health)
            and ("status" not in filters or box.state in filters["status"])
            and all(_has_label(box, term) for term in filters.get("label", []))
            and ("name" not in filters or _named(box, filters["name"]))
        ]
        return json_reply(rows)

    async def _volumes(
        self, method: str, parts: list[str], query: dict[str, str], body: bytes
    ) -> Reply | None:
        if method == "GET" and parts == ["volumes"]:
            documents = [self._volume_document(name) for name in self.volumes]
            return json_reply({"Volumes": documents, "Warnings": None})
        if method == "GET" and len(parts) == 2:
            if parts[1] not in self.volumes:
                return error_reply(404)
            return json_reply(self._volume_document(parts[1]))
        if method == "POST" and parts == ["volumes", "create"]:
            await self._pass_gate("volume_create")
            failed = self._failure("volume_create", None)
            if failed is not None:
                return failed
            return self._volume_create(json.loads(body))
        if method == "DELETE" and len(parts) == 2:
            await self._pass_gate("volume_remove")
            failed = self._failure("volume_remove", parts[1])
            if failed is not None:
                return failed
            return self._volume_remove(parts[1])
        return None

    def _volume_document(self, name: str) -> dict[str, Any]:
        driver, options = self.volume_docs.get(name, ("local", {}))
        labels = dict(self.volume_labels.get(name, {}))
        if self.volumes[name]:
            labels[ANONYMOUS_LABEL] = ""
        document: dict[str, Any] = {
            "Name": name,
            "Driver": driver,
            "Labels": labels,
            "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
            "Scope": "local",
            "CreatedAt": self.volume_created.get(name, VOLUME_CREATED),
        }
        if options:
            document["Options"] = options
        return document

    def _volume_create(self, document: dict[str, Any]) -> Reply:
        name = document["Name"]
        if name in self.volumes:
            return json_reply(self._volume_document(name), 200)
        self.add_volume(
            name,
            driver=document.get("Driver", "local"),
            options=document.get("DriverOpts"),
            labels=document.get("Labels"),
        )
        return json_reply(self._volume_document(name), 201)

    def _volume_remove(self, name: str) -> Reply:
        if name not in self.volumes:
            return error_reply(404)
        if any(m.get("Name") == name for box in self.boxes.values() for m in box.mounts):
            return json_reply({"message": f"{VOLUME_IN_USE} - [{ENGINE_TEXT}]"}, 409)
        del self.volumes[name]
        return Reply(204)

    async def _networks(self, method: str, parts: list[str], body: bytes) -> Reply | None:
        if method == "GET" and parts == ["networks"]:
            return json_reply([self._network_listing(n) for n in self.networks.values()])
        if method == "GET" and len(parts) == 2:
            found = self._find_network(parts[1])
            return error_reply(404) if found is None else json_reply(self._network_inspect(found))
        if method == "POST" and parts == ["networks", "create"]:
            await self._pass_gate("network_create")
            failed = self._failure("network_create", None)
            if failed is not None:
                return failed
            return self._network_create(json.loads(body))
        if method == "POST" and len(parts) == 3 and parts[2] in ("connect", "disconnect"):
            await self._pass_gate(f"network_{parts[2]}")
            failed = self._failure(f"network_{parts[2]}", None)
            if failed is not None:
                return failed
            return self._network_attach(parts[1], parts[2], json.loads(body))
        if method == "DELETE" and len(parts) == 2:
            await self._pass_gate("network_remove")
            failed = self._failure("network_remove", None)
            if failed is not None:
                return failed
            return self._network_remove(parts[1])
        return None

    @staticmethod
    def _network_listing(document: dict[str, Any]) -> dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in document.items() if k != "Containers"}

    def _network_inspect(self, document: dict[str, Any]) -> dict[str, Any]:
        inspected = copy.deepcopy(document)
        inspected["Containers"] = {
            box.id: {
                "Name": box.name,
                "IPv4Address": f"{endpoint.get('IPAddress', '')}/16",
                "IPv6Address": "",
                "MacAddress": endpoint.get("MacAddress", MAC_ADDRESS),
            }
            for box in self.boxes.values()
            if box.state in LIVE_STATES
            for endpoint in box._endpoints().values()
            if endpoint.get("NetworkID") == document["Id"]
        }
        return inspected

    def _network_create(self, document: dict[str, Any]) -> Reply:
        if any(n["Name"] == document["Name"] for n in self.networks.values()):
            return json_reply({"message": f"network with name exists {ENGINE_TEXT}"}, 409)
        self._made_networks += 1
        network_id = created_network_id(self._made_networks)
        overlay = document.get("Driver") == "overlay"
        self.networks[network_id] = _network_document(
            network_id,
            document["Name"],
            document.get("Driver", "bridge"),
            Scope="swarm" if overlay else "local",
            Internal=document.get("Internal", False),
            Attachable=document.get("Attachable", False),
            EnableIPv6=document.get("EnableIPv6", False),
            IPAM=document.get("IPAM", {"Config": []}),
            Options=document.get("Options", {}),
            Labels=document.get("Labels", {}),
        )
        return json_reply({"Id": network_id, "Warning": ""}, 201)

    def _network_attach(self, reference: str, verb: str, document: dict[str, Any]) -> Reply:
        network = self._find_network(reference)
        box = self.resolve(document["Container"])
        if network is None or box is None:
            return json_reply({"message": f"missing {ENGINE_TEXT}"}, 404)
        endpoints = box.endpoints if box.endpoints is not None else box._endpoints()
        box.endpoints = endpoints
        name = network["Name"]
        if verb == "disconnect":
            if name not in endpoints:
                return json_reply({"message": f"not connected {ENGINE_TEXT}"}, 404)
            del endpoints[name]
            return Reply(200)
        if name in endpoints:
            return json_reply({"message": f"endpoint exists {ENGINE_TEXT}"}, 403)
        config = document.get("EndpointConfig", {})
        address = config.get("IPAMConfig", {}).get("IPv4Address", "172.30.0.9")
        endpoints[name] = {
            "NetworkID": network["Id"],
            "Aliases": config.get("Aliases", []),
            "IPAddress": address,
            "Gateway": "172.30.0.1",
            "MacAddress": MAC_ADDRESS,
            **({"IPAMConfig": config["IPAMConfig"]} if "IPAMConfig" in config else {}),
        }
        return Reply(200)

    def _network_remove(self, reference: str) -> Reply:
        network = self._find_network(reference)
        if network is None:
            return error_reply(404)
        active = any(
            network["Id"] == endpoint.get("NetworkID")
            for box in self.boxes.values()
            if box.state in LIVE_STATES
            for endpoint in box._endpoints().values()
        )
        if network["Name"] in PREDEFINED_NETWORKS or active:
            return json_reply({"message": f"forbidden {ENGINE_TEXT}"}, 403)
        del self.networks[network["Id"]]
        return Reply(204)

    def _find_network(self, reference: str) -> dict[str, Any] | None:
        if reference in self.networks:
            return self.networks[reference]
        for network in self.networks.values():
            if network["Name"] == reference:
                return network
        matches = [n for n in self.networks.values() if n["Id"].startswith(reference)]
        return matches[0] if len(matches) == 1 else None

    def _find_image(self, reference: str) -> dict[str, Any] | None:
        if reference in self.images:
            return self.images[reference]
        if reference in self.image_store:
            return self.image_store[reference]
        name = familiar_name(reference)
        for candidate in (name, f"{name}:latest"):
            if candidate in self.images:
                return self.images[candidate]
        for document in self.image_store.values():
            if name in document["RepoDigests"] or any(
                familiar_name(d) == name for d in document["RepoDigests"]
            ):
                return document
        return None

    def _image_list(self) -> Reply:
        failed = self._failure("image_list", None)
        if failed is not None:
            return failed
        rows = [
            {
                "Id": document["Id"],
                "RepoTags": list(document["RepoTags"]) or None,
                "RepoDigests": list(document["RepoDigests"]) or None,
                "Created": _epoch(document["Created"]),
                "Size": document["Size"],
                "Containers": -1,
            }
            for document in self.image_store.values()
        ]
        return json_reply(rows)

    def _image_history(self, reference: str) -> Reply:
        failed = self._failure("image_history", None)
        if failed is not None:
            return failed
        document = self._find_image(unquote(reference))
        if document is None:
            return json_reply({"message": f"No such image: {ENGINE_TEXT}"}, 404)
        layers = document.get("History") or [
            {
                "Id": document["Id"],
                "Created": _epoch(document["Created"]),
                "CreatedBy": "/bin/sh -c #(nop)  CMD",
                "Tags": list(document["RepoTags"]),
                "Size": 0,
            },
            {
                "Id": "<missing>",
                "Created": CREATED,
                "CreatedBy": "ADD rootfs",
                "Tags": [],
                "Size": 1,
            },
        ]
        return json_reply(layers)

    def _image_tag(self, reference: str, query: dict[str, str]) -> Reply:
        failed = self._failure("image_tag", None)
        if failed is not None:
            return failed
        document = self._find_image(unquote(reference))
        if document is None:
            return json_reply({"message": f"No such image: {ENGINE_TEXT}"}, 404)
        self._attach(document, familiar_name(f"{query['repo']}:{query['tag']}"))
        return Reply(201)

    async def _image_push(self, reference: str, query: dict[str, str], scope: Scope) -> Reply:
        await self._pass_gate("push")
        failed = self._failure("image_push", None)
        if failed is not None:
            return failed
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if "x-registry-auth" not in headers:
            return json_reply({"message": f"missing X-Registry-Auth {ENGINE_TEXT}"}, 400)
        name = f"{familiar_name(unquote(reference))}:{query['tag']}"
        document = self.images.get(name)
        if document is None:
            return json_reply({"message": f"tag does not exist: {ENGINE_TEXT}"}, 404)
        if self.push_status is not None:
            return json_reply({"message": f"registry detail {ENGINE_TEXT}"}, self.push_status)
        lines = list(self.push_lines)
        if self.push_error is not None:
            lines.append({"error": self.push_error, "errorDetail": {"message": self.push_error}})
        elif self.push_digest is not None:
            lines.append(
                {
                    "status": "pushed",
                    "aux": {"Tag": query["tag"], "Digest": self.push_digest, "Size": 1},
                }
            )
        body = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
        return Reply(200, body, {"Content-Type": "application/json"})

    def _image_delete(self, reference: str, query: dict[str, str]) -> Reply:
        failed = self._failure("image_remove", None)
        if failed is not None:
            return failed
        name = unquote(reference)
        document = self._find_image(name)
        if document is None:
            return json_reply({"message": f"No such image: {ENGINE_TEXT}"}, 404)
        force = query.get("force") in TRUTHY
        users = [b for b in self.boxes.values() if b.image_id == document["Id"]]
        running = any(b.state in LIVE_STATES for b in users)
        if name in (document["Id"], document["Id"].removeprefix("sha256:")):
            return self._delete_by_id(document, users, running, force)
        return self._delete_reference(document, name, users, running, force)

    def _delete_by_id(
        self, document: dict[str, Any], users: list[Box], running: bool, force: bool
    ) -> Reply:
        if (document["RepoTags"] and not force) or (users and not force) or running:
            return json_reply({"message": f"conflict {ENGINE_TEXT}"}, 409)
        items = [{"Untagged": t} for t in document["RepoTags"]]
        items += [{"Untagged": d} for d in document["RepoDigests"]]
        items.append({"Deleted": document["Id"]})
        self.remove_image(document["Id"])
        return json_reply(items)

    def _delete_reference(
        self, document: dict[str, Any], name: str, users: list[Box], running: bool, force: bool
    ) -> Reply:
        held = [*document["RepoTags"], *document["RepoDigests"]]
        target = next(
            (r for r in held if r == name or familiar_name(r) == familiar_name(name)), None
        )
        if target is None and ":" not in name.rpartition("/")[2]:
            target = next((r for r in held if r == f"{name}:latest"), None)
        if target is None:
            return json_reply({"message": f"No such image: {ENGINE_TEXT}"}, 404)
        dropped = [target]
        tags = [t for t in document["RepoTags"] if t != target]
        digests = [d for d in document["RepoDigests"] if d != target]
        if "@" not in target and not any(repository_of(t) == repository_of(target) for t in tags):
            dropped += [d for d in digests if repository_of(d) == repository_of(target)]
            digests = [d for d in digests if repository_of(d) != repository_of(target)]
        last = not tags and not digests
        if last and users and not force:
            return json_reply({"message": f"conflict {ENGINE_TEXT}"}, 409)
        document["RepoTags"][:] = tags
        document["RepoDigests"][:] = digests
        self.images.pop(target, None)
        items: list[dict[str, str]] = [{"Untagged": d} for d in dropped]
        if last and not running:
            items.append({"Deleted": document["Id"]})
            self.remove_image(document["Id"])
        return json_reply(items)

    def _image(self, reference: str) -> Reply:
        failed = self._failure("image_inspect", None)
        if failed is not None:
            return failed
        found = self._find_image(unquote(reference))
        if found is None:
            return json_reply({"message": f"No such image: {ENGINE_TEXT}"}, 404)
        return json_reply(found)

    def _info(self) -> Reply:
        return json_reply(
            {
                "ID": "daemon",
                "DefaultRuntime": self.default_runtime,
                "LoggingDriver": self.logging_driver,
                "CgroupVersion": self.cgroup_version,
            }
        )

    async def _pull(self, scope: Scope, query: dict[str, str]) -> Reply:
        await self._pass_gate("pull")
        if self.pull_status is not None:
            return json_reply({"message": f"registry detail {ENGINE_TEXT}"}, self.pull_status)
        source = query["fromImage"]
        reference = (
            f"{familiar_name(source)}:{query['tag']}" if "tag" in query else familiar_name(source)
        )
        if "@" in reference:
            reference = f"{familiar_name(repository_of(source))}@{source.partition('@')[2]}"
        digests: tuple[str, ...] = ()
        if self.pull_digest is not None:
            digests = (f"{repository_of(familiar_name(source))}@{self.pull_digest}",)
        self.add_image(reference, self.pull_id, copy.deepcopy(self.pull_config), digests=digests)
        lines = list(self.pull_lines)
        if self.pull_error is not None:
            lines.append({"error": self.pull_error, "errorDetail": {"message": self.pull_error}})
        body = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
        return Reply(200, body, {"Content-Type": "application/json"})

    async def _create(self, query: dict[str, str], body: bytes) -> Reply:
        await self._pass_gate("create")
        failed = self._failure("create", None)
        if failed is not None:
            return failed
        name = query.get("name")
        if name and any(box.name == name for box in self.boxes.values()):
            return json_reply({"message": f"Conflict. {ENGINE_TEXT}"}, 409)
        document = json.loads(body)
        shares_network = str(document.get("HostConfig", {}).get("NetworkMode", "")).startswith(
            "container:"
        )
        if shares_network and (document.get("Hostname") or document.get("Domainname")):
            return json_reply({"message": f"conflicting options: hostname {ENGINE_TEXT}"}, 400)
        self._created += 1
        container_id = created_id(self._created)
        made = self._materialise(
            container_id, name or f"generated-{self._created}", document, "created"
        )
        if isinstance(made, Reply):
            self._created -= 1
            return made
        return json_reply({"Id": container_id, "Warnings": []}, 201)

    def _rename(self, box: Box, query: dict[str, str]) -> Reply:
        failed = self._failure("rename", box.id)
        if failed is not None:
            return failed
        new_name = query["name"]
        if any(other.name == new_name for other in self.boxes.values()):
            return json_reply({"message": f"Conflict. {ENGINE_TEXT}"}, 409)
        box.name = new_name
        return Reply(204)

    def _materialise(
        self, container_id: str, name: str, document: dict[str, Any], state: str
    ) -> Box | Reply:
        image = self._find_image(document["Image"])
        if image is None:
            return json_reply({"message": f"No such image: {ENGINE_TEXT}"}, 404)
        host = {**self._host_defaults(), **copy.deepcopy(document.get("HostConfig", {}))}
        config = self._merged_config(container_id, document, image)
        endpoints = self._endpoints(container_id, document, host)
        if isinstance(endpoints, Reply):
            return endpoints
        box = Box(
            container_id,
            name,
            state=state,
            labels=dict(config.get("Labels", {})),
            image=document["Image"],
            image_id=image["Id"],
            config=config,
            host_config=host,
            endpoints=endpoints,
        )
        box.mounts = self._mounts(host, config)
        self.boxes[container_id] = box
        return box

    def _host_defaults(self) -> dict[str, Any]:
        return {
            "NetworkMode": "bridge",
            "ContainerIDFile": "",
            "LogConfig": {"Type": self.logging_driver, "Config": {}},
            "CgroupnsMode": "private",
            "ShmSize": 67108864,
            "MaskedPaths": list(MASKED_PATHS),
            "ReadonlyPaths": list(READONLY_PATHS),
        }

    @staticmethod
    def _merged_config(
        container_id: str, document: dict[str, Any], image: dict[str, Any]
    ) -> dict[str, Any]:
        config = {
            k: copy.deepcopy(v)
            for k, v in document.items()
            if k not in ("HostConfig", "NetworkingConfig") and v is not None
        }
        defaults = image.get("Config", {})
        own_env = config.get("Env", [])
        own_keys = {entry.partition("=")[0] for entry in own_env}
        env = [e for e in defaults.get("Env", []) if e.partition("=")[0] not in own_keys]
        if env or own_env:
            config["Env"] = [*env, *own_env]
        labels = {**defaults.get("Labels", {}), **config.get("Labels", {})}
        if labels:
            config["Labels"] = labels
        for key in ("ExposedPorts", "Volumes"):
            merged = {**defaults.get(key, {}), **config.get(key, {})}
            if merged:
                config[key] = merged
        for key in IMAGE_ONLY_KEYS:
            if key not in config and key in defaults:
                config[key] = copy.deepcopy(defaults[key])
        config.setdefault("Hostname", container_id[:12])
        return config

    def _endpoints(
        self, container_id: str, document: dict[str, Any], host: dict[str, Any]
    ) -> dict[str, dict[str, Any]] | Reply:
        requested = document.get("NetworkingConfig", {}).get("EndpointsConfig", {})
        endpoints: dict[str, dict[str, Any]] = {}
        for key, endpoint in requested.items():
            network = self._find_network(key)
            if network is None:
                return json_reply({"message": f"network {ENGINE_TEXT} not found"}, 404)
            others = {k: copy.deepcopy(v) for k, v in endpoint.items() if k != "Aliases"}
            endpoints[network["Name"]] = {
                "NetworkID": network["Id"],
                "Aliases": [*endpoint.get("Aliases", []), container_id[:12]],
                "MacAddress": MAC_ADDRESS,
                "IPAddress": "172.18.0.2",
                **others,
            }
        mode = host["NetworkMode"]
        if not endpoints and not str(mode).startswith("container:"):
            network = self._find_network(mode if mode != "default" else "bridge")
            if network is not None:
                endpoints[network["Name"]] = {"NetworkID": network["Id"], "Aliases": []}
        return endpoints

    def _mounts(self, host: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
        mounts: list[dict[str, Any]] = []
        declared: set[str] = set()
        for entry in host.get("Mounts", []):
            declared.add(entry["Target"])
            mounts.append(
                self._mount(entry["Type"], entry.get("Source", ""), entry["Target"], entry)
            )
        for bind in host.get("Binds", []):
            source, _, rest = bind.partition(":")
            target, _, options = rest.partition(":")
            kind = "volume" if VOLUME_NAME.fullmatch(source) else "bind"
            declared.add(target)
            mounts.append(
                self._mount(kind, source, target, {"ReadOnly": "ro" in options.split(",")})
            )
        for target in config.get("Volumes", {}):
            if target not in declared:
                mounts.append(self._mount("volume", "", target, {}))
        return mounts

    def _mount(self, kind: str, source: str, target: str, entry: dict[str, Any]) -> dict[str, Any]:
        writable = not entry.get("ReadOnly", False)
        if kind == "volume":
            name = source or self._new_anonymous()
            self.volumes.setdefault(name, False)
            return {
                "Type": "volume",
                "Name": name,
                "Source": f"/var/lib/docker/volumes/{name}/_data",
                "Destination": target,
                "RW": writable,
            }
        if kind == "bind":
            return {"Type": "bind", "Source": source, "Destination": target, "RW": writable}
        return {"Type": kind, "Source": "", "Destination": target, "RW": writable}

    def _new_anonymous(self) -> str:
        self._anonymous += 1
        name = anonymous_volume(self._anonymous)
        self.volumes[name] = True
        return name

    def _failure(self, verb: str, target: str | None) -> Reply | None:
        failure = self._failures.get(verb)
        if failure is None or failure.target not in (None, target):
            return None
        if failure.skip > 0:
            failure.skip -= 1
            return None
        if failure.times is not None:
            if failure.times <= 0:
                return None
            failure.times -= 1
        return json_reply({"message": ENGINE_TEXT}, failure.status)

    async def _act(self, box: Box, verb: str) -> Reply:
        await self._pass_gate(verb)
        failed = self._failure(verb, box.id)
        if failed is not None:
            return failed
        handler = _TRANSITIONS.get(verb)
        if handler is None:
            return error_reply(404)
        return handler(box)

    async def _pass_gate(self, name: str) -> None:
        hook = self._hooks.get(name)
        if hook is not None:
            pending = hook()
            if pending is not None:
                await pending
        gate = self._gates.get(name)
        if gate is not None:
            gate.entered.set()
            await gate.release.wait()

    def _remove(self, box: Box, query: dict[str, str]) -> Reply:
        failed = self._failure("remove", box.id)
        if failed is not None:
            return failed
        if box.state in LIVE_STATES and query.get("force") not in TRUTHY:
            return json_reply({"message": ENGINE_TEXT}, 409)
        del self.boxes[box.id]
        return Reply(204)


def _network_document(network_id: str, name: str, driver: str, **fields: Any) -> dict[str, Any]:
    return {
        "Id": network_id,
        "Name": name,
        "Driver": driver,
        "Scope": "local",
        "Created": NETWORK_CREATED,
        "IPAM": {"Config": []},
        "Labels": {},
        "Options": {},
        **fields,
    }


def _has_label(box: Box, term: str) -> bool:
    key, separator, value = term.partition("=")
    return key in box.labels and (not separator or box.labels[key] == value)


def _named(box: Box, patterns: list[str]) -> bool:
    return any(re.search(pattern, f"/{box.name}") for pattern in patterns)


def _epoch(text: str) -> int:
    return int(datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp())


def _conflict() -> Reply:
    return json_reply({"message": ENGINE_TEXT}, 409)


def _start(box: Box) -> Reply:
    if box.state == "running":
        return Reply(304)
    if box.state in ("paused", "removing"):
        return _conflict()
    box.state = "running"
    return Reply(204)


def _stop(box: Box) -> Reply:
    if box.state not in LIVE_STATES:
        return Reply(304)
    box.state = "exited"
    return Reply(204)


def _restart(box: Box) -> Reply:
    box.state = "running"
    return Reply(204)


def _kill(box: Box) -> Reply:
    if box.state not in LIVE_STATES:
        return _conflict()
    box.state = "exited"
    return Reply(204)


def _pause(box: Box) -> Reply:
    if box.state != "running":
        return _conflict()
    box.state = "paused"
    return Reply(204)


def _unpause(box: Box) -> Reply:
    if box.state != "paused":
        return _conflict()
    box.state = "running"
    return Reply(204)


_TRANSITIONS: dict[str, Callable[[Box], Reply]] = {
    "start": _start,
    "stop": _stop,
    "restart": _restart,
    "kill": _kill,
    "pause": _pause,
    "unpause": _unpause,
}
