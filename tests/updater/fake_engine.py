"""A Docker Engine on a Unix socket, for the update helper's standard-library HTTP client."""

import json
import re
import socketserver
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

API_VERSION = "1.47"
OLD_ID = "a" * 64
OLD_IMAGE_ID = "sha256:" + "1" * 64
NEW_IMAGE_ID = "sha256:" + "b" * 64
OTHER_IMAGE_ID = "sha256:" + "8" * 64
NEW_REFERENCE = "ghcr.io/example/dockmin:0.0.2"
NAME = "dockmin"
NEW_DATABASE_SUFFIX = b"+new"
NEW_WAL = b"wal-from-new"
LOG_LINES = 300
LONG_LINE_BYTES = 5000
EXIT_CODE = 3
SHUTDOWN_POLL_SECONDS = 0.01

type Behaviour = Literal["healthy", "starting", "exit", "unhealthy", "nohealth", "nohealth_dies"]
type Verb = Literal["inspect", "image", "create", "stop", "start", "rename", "logs", "remove"]
type Target = Literal["any", "old", "new"]
type CreateMode = Literal["normal", "dropped", "dropped_foreign"]


@dataclass(slots=True)
class FakeContainer:
    """One container as the fake daemon holds it; `body` is set for those the helper created."""

    id: str
    name: str
    running: bool = True
    health: str | None = None
    polls: int = 0
    body: dict[str, Any] | None = None
    exited: bool = False
    foreign: bool = False
    tty: bool = False

    @property
    def is_new(self) -> bool:
        return self.body is not None


@dataclass(frozen=True, slots=True)
class Call:
    """One request the daemon received, with the version prefix removed from the path."""

    method: str
    path: str
    query: dict[str, str]

    @property
    def verb(self) -> str:
        if self.method == "DELETE":
            return "remove"
        return self.path.rsplit("/", 1)[-1]


@dataclass(slots=True)
class Failure:
    """An error answer for one verb, optionally only for the old or the new container."""

    status: int
    message: str
    target: Target = "any"


@dataclass(slots=True)
class Hook:
    """Code run once in the daemon's thread as a matching request arrives, after `skip` of them."""

    action: Callable[[], None]
    target: Target = "any"
    skip: int = 0
    spent: bool = False


@dataclass(slots=True)
class FakeDaemon:
    """The daemon's state and the knobs a test turns."""

    data_dir: Path
    behaviour: Behaviour = "healthy"
    healthy_after: int = 2
    created_image: str = NEW_IMAGE_ID
    create_mode: CreateMode = "normal"
    wrong_id_for: str | None = None
    create_reply: bytes | None = None
    new_tty: bool = False
    calls: list[Call] = field(default_factory=list)
    failures: dict[Verb, Failure] = field(default_factory=dict)
    hooks: dict[Verb, Hook] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=lambda: {NEW_REFERENCE: NEW_IMAGE_ID})
    containers: dict[str, FakeContainer] = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock)
    counter: int = 0

    def __post_init__(self) -> None:
        self.containers = {OLD_ID: FakeContainer(OLD_ID, f"/{NAME}", health="healthy")}

    def find(self, reference: str) -> FakeContainer | None:
        if reference in self.containers:
            return self.containers[reference]
        for found in self.containers.values():
            if found.name == f"/{reference}":
                return found
        return None

    def names(self) -> list[tuple[str, bool]]:
        """Return each container's name and whether it runs, sorted."""
        return sorted((c.name, c.running) for c in self.containers.values())

    def mutations(self) -> list[Call]:
        """Return every request that is not a read."""
        return [call for call in self.calls if call.method != "GET"]

    def verbs(self) -> list[str]:
        """Return the verb of every mutating request, in order."""
        return [call.verb for call in self.mutations()]

    def newest(self) -> FakeContainer:
        """Return the one container the helper created."""
        [created] = [c for c in self.containers.values() if c.is_new]
        return created


def _applies(target: Target, container: FakeContainer | None) -> bool:
    if target == "any":
        return True
    return container is not None and container.is_new == (target == "new")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    daemon: FakeDaemon

    def log_message(self, format: str, *args: Any) -> None:
        return

    def address_string(self) -> str:
        return "unix"

    def reply(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def error(self, status: int, message: str) -> None:
        self.reply(status, json.dumps({"message": message}).encode())

    def handle_any(self) -> None:
        daemon = self.daemon
        parts = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        prefix = re.fullmatch(rf"/v{re.escape(API_VERSION)}(/.*)", parts.path)
        if prefix is None:
            self.error(404, "unsupported API version")
            return
        path = prefix.group(1)
        with daemon.lock:
            daemon.calls.append(Call(self.command, path, query))
            self.route(self.command, path, query, raw)

    def verb_of(self, method: str, path: str) -> tuple[Verb | None, FakeContainer | None]:
        if method == "GET" and re.fullmatch(r"/images/.+/json", path):
            return "image", None
        if method == "POST" and path == "/containers/create":
            return "create", None
        found = re.fullmatch(r"/containers/([^/]+)(?:/(json|stop|start|rename|logs))?", path)
        if found is None:
            return None, None
        container = self.daemon.find(unquote(found.group(1)))
        action = found.group(2)
        if method == "DELETE" and action is None:
            return "remove", container
        names: dict[str, Verb] = {
            "json": "inspect",
            "stop": "stop",
            "start": "start",
            "rename": "rename",
            "logs": "logs",
        }
        return (names.get(action or ""), container)

    @staticmethod
    def reference_of(path: str) -> str:
        return unquote(path.split("/")[2])

    def route(self, method: str, path: str, query: dict[str, str], raw: bytes) -> None:
        daemon = self.daemon
        verb, container = self.verb_of(method, path)
        if verb is not None:
            hook = daemon.hooks.get(verb)
            if hook is not None and not hook.spent and _applies(hook.target, container):
                if hook.skip > 0:
                    hook.skip -= 1
                else:
                    hook.spent = True
                    hook.action()
            failure = daemon.failures.get(verb)
            if failure is not None and _applies(failure.target, container):
                self.error(failure.status, failure.message)
                return
        if verb == "image":
            self.image(path)
        elif verb == "create":
            self.create(query, raw)
        elif verb is None or container is None:
            self.error(404, f"No such container: {path}")
        elif verb == "inspect":
            self.inspect(container, self.reference_of(path))
        elif verb == "stop":
            self.stop(container)
        elif verb == "start":
            self.start(container)
        elif verb == "rename":
            self.rename(container, query)
        elif verb == "logs":
            self.logs(container)
        elif verb == "remove":
            self.remove(container, query)

    def image(self, path: str) -> None:
        reference = unquote(path.removeprefix("/images/").removesuffix("/json"))
        image_id = self.daemon.images.get(reference)
        if image_id is None:
            self.error(404, f"No such image: {reference}")
            return
        self.reply(200, json.dumps({"Id": image_id}).encode())

    def create(self, query: dict[str, str], raw: bytes) -> None:
        daemon = self.daemon
        name = query["name"]
        existing = daemon.find(name)
        if existing is not None and existing.name == f"/{name}":
            self.error(409, "Conflict. The container name is already in use")
            return
        daemon.counter += 1
        created = FakeContainer(
            f"{daemon.counter:x}".rjust(64, "c"),
            f"/{name}",
            running=False,
            body=json.loads(raw),
            foreign=daemon.create_mode == "dropped_foreign",
            tty=daemon.new_tty,
        )
        daemon.containers[created.id] = created
        if daemon.create_mode != "normal":
            self.close_connection = True
            return
        if daemon.create_reply is not None:
            self.reply(201, daemon.create_reply)
            return
        self.reply(201, json.dumps({"Id": created.id, "Warnings": []}).encode())

    def stop(self, container: FakeContainer) -> None:
        if not container.running:
            self.reply(304)
            return
        container.running = False
        self.reply(204)

    def start(self, container: FakeContainer) -> None:
        if container.running:
            self.reply(304)
            return
        container.running = True
        if container.is_new:
            self.new_started(container)
        self.reply(204)

    def rename(self, container: FakeContainer, query: dict[str, str]) -> None:
        name = query["name"]
        other = self.daemon.find(name)
        if other is not None and other.name == f"/{name}":
            self.error(409, "Conflict. The container name is already in use")
            return
        container.name = f"/{name}"
        self.reply(204)

    def remove(self, container: FakeContainer, query: dict[str, str]) -> None:
        daemon = self.daemon
        if container.running and query.get("force") != "1":
            self.error(409, "cannot remove a running container")
            return
        assert query.get("v") == "0", "a removal must never take volumes"
        del daemon.containers[container.id]
        self.reply(204)

    def new_started(self, container: FakeContainer) -> None:
        data = self.daemon.data_dir
        (data / "dockmin.db").write_bytes((data / "dockmin.db").read_bytes() + NEW_DATABASE_SUFFIX)
        (data / "dockmin.db-wal").write_bytes(NEW_WAL)
        match self.daemon.behaviour:
            case "exit":
                container.running = False
                container.exited = True
            case "unhealthy":
                container.health = "unhealthy"
            case "nohealth" | "nohealth_dies":
                container.health = None
            case _:
                container.health = "starting"

    def inspect(self, container: FakeContainer, requested: str) -> None:
        daemon = self.daemon
        container.polls += 1
        health = container.health
        if container.is_new and container.running:
            if daemon.behaviour == "healthy" and container.polls > daemon.healthy_after:
                health = container.health = "healthy"
            dies = daemon.behaviour == "nohealth_dies" and container.polls >= 4
            if dies:
                container.running = False
                container.exited = True
        state: dict[str, Any] = {
            "Running": container.running,
            "Status": "running" if container.running else "exited",
            "ExitCode": EXIT_CODE if container.exited else 0,
        }
        if health is not None:
            state["Health"] = {"Status": health}
        config: dict[str, Any] = {"Tty": container.tty}
        if container.is_new and not daemon.behaviour.startswith("nohealth"):
            config["Healthcheck"] = {"Test": ["CMD", "true"]}
        if not container.is_new:
            image = OLD_IMAGE_ID
        elif container.foreign:
            image = OTHER_IMAGE_ID
        else:
            image = daemon.created_image
        identifier = "d" * 64 if daemon.wrong_id_for == requested else container.id
        created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
        document = {
            "Id": identifier,
            "Name": container.name,
            "Image": image,
            "Created": created,
            "State": state,
            "Config": config,
        }
        self.reply(200, json.dumps(document).encode())

    def logs(self, container: FakeContainer) -> None:
        if container.tty:
            raw = b"".join(f"raw {i}\n".encode() for i in range(LOG_LINES))
            self.reply(200, raw, "application/vnd.docker.raw-stream")
            return
        out = bytearray()
        for i in range(LOG_LINES):
            out += _frame(1, f"line {i}\n".encode())
        out += _frame(2, b"x" * LONG_LINE_BYTES + b"\n")
        out += _frame(2, b"par")
        out += _frame(2, b"tial\n")
        self.reply(200, bytes(out), "application/vnd.docker.multiplexed-stream")

    do_GET = do_POST = do_DELETE = handle_any


def _frame(stream: int, payload: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self) -> tuple[Any, Any]:
        request, _ = super().get_request()
        return request, ("unix", 0)


@contextmanager
def serving(daemon: FakeDaemon, socket_path: Path) -> Iterator[None]:
    """Serve the daemon on the socket for the block; the thread and the socket end with it."""
    handler = type("Handler", (_Handler,), {"daemon": daemon})
    server = _Server(str(socket_path), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": SHUTDOWN_POLL_SECONDS}, daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        socket_path.unlink(missing_ok=True)
