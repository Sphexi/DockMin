"""HTTP plumbing for one environment: negotiation, versioned paths, and error mapping."""

import asyncio
import json
import re
import unicodedata
from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ValidationError

from app.docker_engine.errors import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    PermissionDenied,
    RequestOutcomeUnknown,
    ResponseTooLarge,
    VersionUnsupported,
)
from app.docker_engine.models import DaemonInfo, SwarmState
from app.docker_engine.timeouts import Timeouts
from app.docker_engine.transport import (
    ConnectionSettings,
    EndpointAddressRefused,
    HijackedConnection,
    HijackError,
    TransportBundle,
    _RefusedConnect,
    open_hijack,
)
from app.docker_engine.versioning import (
    API_VERSION_FLOOR,
    CLIENT_MAX_API_VERSION,
    format_api_version,
    parse_api_version,
)
from app.netpolicy import REFUSAL_TEXT

type Params = Mapping[str, str | int | bool]

_PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_REQUEST_TARGET = re.compile(r"[\x21-\x7e]+")
_IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*")
_GATEWAY_STATUSES = frozenset({502, 504})
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
COUNT_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CONCURRENT_LIST_DECODES = 2
_MAX_ENGINE_MESSAGE_CHARS = 200
_MAX_ENGINE_ERROR_BODY_BYTES = 64 * 1024
NEGOTIATION_REPLY_BYTES = 64 * 1024
ACTION_REPLY_BYTES = 64 * 1024
# Estimated heap bytes per container, per string and per separator of a JSON document.
_ESTIMATE_PER_CONTAINER = 232
_ESTIMATE_PER_STRING = 56
_ESTIMATE_PER_SEPARATOR = 8
# Heap one decode may occupy; two decodes run at once.
MAX_DECODE_ESTIMATE_BYTES = 128 * 1024 * 1024
# Headers that describe the wire encoding, not an already-decoded body.
_FRAMING_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


_list_decode_gate_state: list[tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = []


def _list_decode_gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if not _list_decode_gate_state or _list_decode_gate_state[0][0] is not loop:
        _list_decode_gate_state[:] = [(loop, asyncio.Semaphore(MAX_CONCURRENT_LIST_DECODES))]
    return _list_decode_gate_state[0][1]


def decode_estimate(content: bytes) -> int:
    """Estimate the heap a JSON document occupies once decoded, from its structure counts."""
    containers = content.count(b"{") + content.count(b"[")
    strings = content.count(b'"') // 2
    return (
        containers * _ESTIMATE_PER_CONTAINER
        + strings * _ESTIMATE_PER_STRING
        + content.count(b",") * _ESTIMATE_PER_SEPARATOR
    )


def _engine_message(body: bytes) -> str | None:
    """Extract the top-level `message` of a JSON error body, normalised for safe storage."""
    if len(body) > _MAX_ENGINE_ERROR_BODY_BYTES:
        return None
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None
    message = document.get("message") if isinstance(document, dict) else None
    if not isinstance(message, str):
        return None
    # Truncate first so the control-character pass never walks an attacker-sized string.
    kept = "".join(
        c
        for c in message[:_MAX_ENGINE_MESSAGE_CHARS]
        if unicodedata.category(c) not in {"Cc", "Zl", "Zp"}
    )
    return kept or None


class AddressRefusedUnreachable(EnvironmentUnreachable):
    """The address policy refuses the Engine endpoint, so no connection was made.

    Retrying cannot succeed until the endpoint or the policy changes.
    """

    kind = "address_refused"

    def __init__(self, environment_id: int) -> None:
        super().__init__(environment_id, REFUSAL_TEXT)


class EngineHijack(HijackedConnection):
    """A hijacked connection that its client closes along with itself."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        content_type: str,
        release: Callable[["EngineHijack"], None],
    ) -> None:
        super().__init__(reader, writer, content_type)
        self._release = release

    async def close(self) -> None:
        """Close the stream and stop the client tracking it; safe to call more than once."""
        self._release(self)
        await super().close()


class _ObservedSettings(ConnectionSettings):
    """Delegates to real settings and keeps the stream they opened, for the caller to adopt."""

    def __init__(self, inner: ConnectionSettings) -> None:
        self._inner = inner
        self.stream: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None

    def create_transport(self) -> TransportBundle:
        return self._inner.create_transport()

    async def open_stream(
        self, connect_timeout: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.stream = await self._inner.open_stream(connect_timeout)
        return self.stream

    @property
    def host_header(self) -> str:
        return self._inner.host_header


def _query_string(params: Params) -> str:
    return urlencode(
        [(k, str(v).lower() if isinstance(v, bool) else str(v)) for k, v in params.items()]
    )


class EngineHttp:
    """One httpx client bound to one environment, with the negotiated version pinned into paths."""

    def __init__(
        self,
        environment_id: int,
        bundle: TransportBundle,
        settings: ConnectionSettings | None = None,
    ) -> None:
        self.environment_id = environment_id
        self._client = httpx.AsyncClient(
            transport=bundle.transport,
            base_url=bundle.base_url,
            timeout=Timeouts.standard().to_httpx(),
            follow_redirects=False,
            # Proxy variables must not redirect traffic meant for a specific Docker host.
            trust_env=False,
        )
        self._pinned: str | None = None
        self._daemon: DaemonInfo | None = None
        self._negotiation_lock = asyncio.Lock()
        self._closed = False
        self._settings = settings
        self._hijacks: dict[int, EngineHijack] = {}

    def __repr__(self) -> str:
        return f"EngineHttp(environment_id={self.environment_id})"

    @property
    def closed(self) -> bool:
        """Report whether aclose has been called."""
        return self._closed

    @property
    def daemon(self) -> DaemonInfo | None:
        """Return what negotiation learned, or None before it has succeeded."""
        return self._daemon

    @property
    def open_hijack_count(self) -> int:
        """Return how many hijacked connections are open and not yet closed by their holders."""
        return len(self._hijacks)

    async def aclose(self) -> None:
        """Close the client and every connection it holds, including streams and hijacks."""
        if self._closed:
            return
        self._closed = True
        hijacks = list(self._hijacks.values())
        try:
            await asyncio.gather(*(hijack.close() for hijack in hijacks))
        finally:
            await self._client.aclose()

    def segment(self, value: str, *, image_ref: bool = False) -> str:
        """Validate an identifier before it is placed in a URL path."""
        pattern = _IMAGE_REF if image_ref else _PLAIN_ID
        if pattern.fullmatch(value) is None or ".." in value.split("/"):
            raise EngineError(self.environment_id, "invalid resource identifier")
        return value

    async def ensure_negotiated(self) -> DaemonInfo:
        """Negotiate on first use; later calls return the recorded result."""
        if self._daemon is not None:
            return self._daemon
        async with self._negotiation_lock:
            if self._daemon is None:
                self._daemon = await self._negotiate()
            return self._daemon

    async def negotiate(self) -> DaemonInfo:
        """Run negotiation again and replace the recorded result."""
        async with self._negotiation_lock:
            self._daemon = await self._negotiate()
            return self._daemon

    async def _negotiate(self) -> DaemonInfo:
        env = self.environment_id
        ping = await self._send(
            "GET", "/_ping", timeouts=Timeouts.standard(), max_bytes=NEGOTIATION_REPLY_BYTES
        )
        raw_api = ping.headers.get("api-version")
        if raw_api is None:
            raise EngineError(env, "endpoint did not report an Engine API version")
        try:
            daemon_api = parse_api_version(raw_api)
        except ValueError:
            raise EngineError(env, "endpoint reported a malformed Engine API version") from None
        if daemon_api < API_VERSION_FLOOR:
            raise VersionUnsupported(
                env,
                f"daemon API {raw_api} is below the supported minimum "
                f"{format_api_version(API_VERSION_FLOOR)}",
                daemon_api_version=raw_api,
            )

        pinned = format_api_version(min(daemon_api, CLIENT_MAX_API_VERSION))
        document = await self._send(
            "GET",
            f"/v{pinned}/version",
            timeouts=Timeouts.standard(),
            max_bytes=NEGOTIATION_REPLY_BYTES,
        )
        body = self._json(document, "GET", "/version")
        engine_version = str(body.get("Version", "")) if isinstance(body, dict) else ""
        raw_min = body.get("MinAPIVersion") if isinstance(body, dict) else None
        if isinstance(raw_min, str):
            try:
                minimum = parse_api_version(raw_min)
            except ValueError:
                raise EngineError(env, "daemon reported a malformed minimum API version") from None
            if minimum > CLIENT_MAX_API_VERSION:
                raise VersionUnsupported(
                    env,
                    f"daemon requires API {raw_min} or later; this client supports up to "
                    f"{format_api_version(CLIENT_MAX_API_VERSION)}",
                    daemon_api_version=raw_api,
                )

        self._pinned = pinned
        return DaemonInfo(
            engine_version=engine_version,
            daemon_api_version=raw_api,
            min_api_version=raw_min if isinstance(raw_min, str) else None,
            negotiated_api_version=pinned,
            swarm_state=SwarmState.from_header(ping.headers.get("swarm")),
        )

    def _url(self, path: str) -> str:
        # Negotiation has already pinned a version before any caller reaches here.
        return f"/v{self._pinned}{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Params | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeouts: Timeouts | None = None,
        also_ok: frozenset[int] = frozenset(),
        max_bytes: int | None = None,
    ) -> httpx.Response:
        """Send a versioned request and return the response; map every failure.

        `content` is a body the caller already encoded, so a large document is serialised off
        the event loop; the caller sets its Content-Type.
        """
        await self.ensure_negotiated()
        return await self._send(
            method,
            self._url(path),
            params=params,
            json_body=json_body,
            content=content,
            headers=headers,
            timeouts=timeouts,
            also_ok=also_ok,
            label=path,
            max_bytes=max_bytes,
        )

    async def get_json(
        self,
        path: str,
        *,
        params: Params | None = None,
        timeouts: Timeouts | None = None,
        max_bytes: int | None = None,
    ) -> Any:
        """GET a versioned path and return its decoded JSON."""
        response = await self.request(
            "GET", path, params=params, timeouts=timeouts, max_bytes=max_bytes
        )
        return self._json(response, "GET", path)

    async def count_items(
        self,
        path: str,
        *,
        key: str | None = None,
        params: Params | None = None,
        max_bytes: int = COUNT_RESPONSE_BYTES,
    ) -> int:
        """Count the entries of a listing without validating them.

        The body is decoded in a worker thread and only the length of the array is kept, at the
        top level or under `key`. Raises ResponseTooLarge past `max_bytes`.
        """
        response = await self.request("GET", path, params=params, max_bytes=max_bytes)
        document = await asyncio.to_thread(self._loads, response.content, "GET", path, response)
        items = document.get(key) if key is not None and isinstance(document, dict) else document
        if items is None:
            return 0
        if not isinstance(items, list):
            raise EngineError(self.environment_id, f"GET {path} did not return a list")
        return len(items)

    async def get_parsed[M: BaseModel](
        self,
        model: type[M],
        path: str,
        *,
        max_bytes: int,
        params: Params | None = None,
        timeouts: Timeouts | None = None,
        prepare: Callable[[Any], Any] | None = None,
    ) -> M:
        """GET a path and validate its JSON object against a model, decoding off the event loop.

        `prepare` reshapes the decoded document in the worker thread before validation.
        Raises ResponseTooLarge past `max_bytes`.
        """
        response = await self.request(
            "GET", path, params=params, timeouts=timeouts, max_bytes=max_bytes
        )
        return await asyncio.to_thread(self._decode_model, model, response, path, prepare)

    async def get_parsed_list[M: BaseModel](
        self,
        model: type[M],
        path: str,
        *,
        max_bytes: int,
        params: Params | None = None,
        timeouts: Timeouts | None = None,
        prepare: Callable[[Any], Any] | None = None,
    ) -> list[M]:
        """GET a path and validate its JSON array against a model, decoding off the event loop.

        At most two list reads run at once in the process, the gate covering the body read as
        well as the decode. `prepare` reshapes the decoded document in the worker thread before
        validation. Raises ResponseTooLarge past `max_bytes` or when the decoded document would
        exceed `MAX_DECODE_ESTIMATE_BYTES`; a null body is an empty list.
        """
        async with _list_decode_gate():
            response = await self.request(
                "GET", path, params=params, timeouts=timeouts, max_bytes=max_bytes
            )
            return await asyncio.to_thread(self._decode_models, model, response, path, prepare)

    async def get_ids(
        self,
        path: str,
        *,
        max_bytes: int,
        params: Params | None = None,
        key: str = "Id",
    ) -> frozenset[str]:
        """GET a path and return the `key` string of each entry of its JSON array, unvalidated.

        Shares the list-read limit. Raises ResponseTooLarge past `max_bytes` or when the decoded
        document would exceed `MAX_DECODE_ESTIMATE_BYTES`.
        """
        async with _list_decode_gate():
            response = await self.request("GET", path, params=params, max_bytes=max_bytes)
            return await asyncio.to_thread(self._decode_ids, response, path, key)

    def _decode_model[M: BaseModel](
        self,
        model: type[M],
        response: httpx.Response,
        path: str,
        prepare: Callable[[Any], Any] | None,
    ) -> M:
        document = self._loads(response.content, "GET", path, response)
        return self.parse(model, document if prepare is None else prepare(document))

    def _decode_models[M: BaseModel](
        self,
        model: type[M],
        response: httpx.Response,
        path: str,
        prepare: Callable[[Any], Any] | None,
    ) -> list[M]:
        document = self._loads(response.content, "GET", path, response)
        return self.parse_list(model, document if prepare is None else prepare(document))

    def _decode_ids(self, response: httpx.Response, path: str, key: str) -> frozenset[str]:
        document = self._loads(response.content, "GET", path, response)
        if document is None:
            return frozenset()
        if not isinstance(document, list):
            raise EngineError(self.environment_id, f"GET {path} did not return a list")
        return frozenset(
            value
            for item in document
            if isinstance(item, dict) and isinstance(value := item.get(key), str)
        )

    def _loads(self, content: bytes, method: str, path: str, response: httpx.Response) -> Any:
        if decode_estimate(content) > MAX_DECODE_ESTIMATE_BYTES:
            raise ResponseTooLarge(self.environment_id, f"{method} {path} response is too large")
        try:
            return json.loads(content)
        except (ValueError, RecursionError):
            raise EngineError(
                self.environment_id,
                f"{method} {path} returned a body that is not JSON",
                status_code=response.status_code,
            ) from None

    def _json(self, response: httpx.Response, method: str, path: str) -> Any:
        try:
            return response.json()
        except ValueError:
            raise EngineError(
                self.environment_id,
                f"{method} {path} returned a body that is not JSON",
                status_code=response.status_code,
            ) from None

    def parse[M: BaseModel](self, model: type[M], data: Any) -> M:
        """Validate decoded JSON against a model; the failing input is never echoed."""
        try:
            return model.model_validate(data)
        except ValidationError:
            raise EngineError(
                self.environment_id, f"response did not match the expected {model.__name__} shape"
            ) from None

    def parse_list[M: BaseModel](self, model: type[M], data: Any) -> list[M]:
        """Validate a decoded JSON array (or null) against a model."""
        if data is None:
            return []
        if not isinstance(data, list):
            raise EngineError(self.environment_id, f"expected a list of {model.__name__}")
        return [self.parse(model, item) for item in data]

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Params | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeouts: Timeouts | None = None,
        also_ok: frozenset[int] = frozenset(),
        label: str | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        label = label if label is not None else url
        if self._closed:
            raise EnvironmentUnreachable(self.environment_id, "client is closed")
        # The mapped error is raised after the handler exits: raising inside it would leave the
        # live httpx request, and its X-Registry-Auth header, reachable through __context__.
        mapped: EngineError | None = None
        try:
            request = self._client.build_request(
                method,
                url,
                params=params,
                json=json_body,
                content=content,
                headers=headers,
                timeout=(timeouts or Timeouts.standard()).to_httpx(),
            )
            streamed = await self._client.send(request, stream=True)
            try:
                body = await self._read_capped(streamed, method, label, max_bytes)
            finally:
                await streamed.aclose()
        except (httpx.HTTPError, httpx.StreamError) as exc:
            mapped = self._transport_error(exc, method, label)
            del exc
        except RuntimeError as exc:
            # httpx raises a bare RuntimeError when the client is closed under an in-flight call.
            if not self._closed:
                raise
            mapped = self._transport_error(exc, method, label)
            del exc
        if mapped is not None:
            raise mapped
        self._check_status(streamed.status_code, method, label, also_ok, body)
        return httpx.Response(
            streamed.status_code,
            headers=[
                (k, v)
                for k, v in streamed.headers.multi_items()
                if k.lower() not in _FRAMING_HEADERS
            ],
            content=body,
            request=request,
        )

    async def _read_capped(
        self, response: httpx.Response, method: str, label: str, max_bytes: int | None
    ) -> bytes:
        limit = _MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
        message = f"{method} {label} response is too large"
        oversize = (
            EngineError(self.environment_id, message)
            if max_bytes is None
            else ResponseTooLarge(self.environment_id, message)
        )
        declared = response.headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared[:18]) > limit:
            raise oversize
        chunks: list[bytes] = []
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > limit:
                raise oversize
            chunks.append(chunk)
        return b"".join(chunks)

    async def stream_bytes(
        self,
        method: str,
        path: str,
        *,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
        timeouts: Timeouts | None = None,
        on_headers: Callable[[Mapping[str, str]], None] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Yield response chunks as they arrive; wrap in `contextlib.aclosing` to release early.

        `on_headers` receives the response headers once the status is accepted, before the first
        chunk is yielded.
        """
        await self.ensure_negotiated()
        if self._closed:
            raise EnvironmentUnreachable(self.environment_id, "client is closed")
        mapped: EngineError | None = None
        try:
            async with self._client.stream(
                method,
                self._url(path),
                params=params,
                headers=headers,
                timeout=(timeouts or Timeouts.stream()).to_httpx(),
            ) as response:
                # The error body is never read: it is not needed and may echo credentials.
                self._check_status(response.status_code, method, path, frozenset())
                if on_headers is not None:
                    on_headers(response.headers)
                async for chunk in response.aiter_bytes():
                    yield chunk
        except (httpx.HTTPError, httpx.StreamError) as exc:
            mapped = self._transport_error(exc, method, path)
            del exc
        except RuntimeError as exc:
            if not self._closed:
                raise
            mapped = self._transport_error(exc, method, path)
            del exc
        if mapped is not None:
            raise mapped

    async def hijack(
        self, path: str, *, params: Params | None = None, body: bytes = b""
    ) -> HijackedConnection:
        """POST a versioned request that the Engine switches to a raw bidirectional stream.

        Uses the settings the client was built with, so TLS verification is the transport's.
        The connection is tracked, and `aclose` closes it. `body` is sent as JSON. Without
        settings the call fails with EnvironmentUnreachable. A failure after the request was
        written is RequestOutcomeUnknown, as the Engine may already have acted on it.
        """
        env = self.environment_id
        if self._settings is None:
            raise EnvironmentUnreachable(env, "client has no connection settings for a raw stream")
        await self.ensure_negotiated()
        if self._closed:
            raise EnvironmentUnreachable(env, "client is closed")
        try:
            target = self._url(path) + (f"?{_query_string(params)}" if params else "")
        except UnicodeError:
            raise EngineError(env, "invalid request parameter") from None
        if _REQUEST_TARGET.fullmatch(target) is None:
            raise EngineError(env, "invalid request target")

        observed = _ObservedSettings(self._settings)
        mapped: EngineError | None = None
        opened: HijackedConnection | None = None
        try:
            opened = await open_hijack(observed, target, body=body)
        except HijackError as exc:
            mapped = self._hijack_refusal(exc, path, sent=observed.stream is not None)
            del exc
        except OSError as exc:
            # Before ValueError: a failed certificate check is both.
            mapped = self._hijack_failure(exc, path, sent=observed.stream is not None)
            del exc
        except ValueError:
            mapped = EngineError(env, f"POST {path} could not be written")
        if mapped is not None:
            raise mapped
        if opened is None or observed.stream is None:
            raise EngineError(env, f"POST {path} did not return a stream")

        reader, writer = observed.stream
        connection = EngineHijack(reader, writer, opened.content_type, self._forget)
        if self._closed:
            await connection.close()
            raise EnvironmentUnreachable(env, "client was closed during the request")
        self._hijacks[id(connection)] = connection
        return connection

    def _forget(self, connection: EngineHijack) -> None:
        self._hijacks.pop(id(connection), None)

    def _hijack_refusal(self, exc: HijackError, label: str, *, sent: bool) -> EngineError:
        env = self.environment_id
        if self._closed:
            return EnvironmentUnreachable(env, "client was closed during the request")
        if exc.status is None:
            if sent:
                return RequestOutcomeUnknown(env, f"POST {label} got no usable answer")
            return EnvironmentUnreachable(env, f"POST {label} lost the connection")
        refused = self._status_error(exc.status, "POST", label, exc.body)
        return refused or EngineError(env, f"POST {label} was not upgraded", status_code=exc.status)

    def _hijack_failure(self, exc: OSError, label: str, *, sent: bool) -> EngineError:
        env = self.environment_id
        if self._closed:
            return EnvironmentUnreachable(env, "client was closed during the request")
        if isinstance(exc, EndpointAddressRefused):
            return AddressRefusedUnreachable(env)
        name = type(exc).__name__
        if sent:
            return RequestOutcomeUnknown(env, f"POST {label} lost the connection ({name})")
        return EnvironmentUnreachable(env, f"POST {label} could not connect ({name})")

    def _transport_error(self, exc: Exception, method: str, label: str) -> EngineError:
        env = self.environment_id
        if self._closed:
            return EnvironmentUnreachable(env, "client was closed during the request")
        name = type(exc).__name__
        match exc:
            case _RefusedConnect():
                return AddressRefusedUnreachable(env)
            case httpx.ReadTimeout() | httpx.WriteTimeout():
                return RequestOutcomeUnknown(env, f"{method} {label} timed out ({name})")
            case httpx.TimeoutException():
                return EnvironmentUnreachable(env, f"{method} {label} timed out ({name})")
            case httpx.TransportError():
                return EnvironmentUnreachable(env, f"{method} {label} lost the connection ({name})")
            case _:
                return EngineError(env, f"{method} {label} failed ({name})")

    def _check_status(
        self,
        status: int,
        method: str,
        label: str,
        also_ok: frozenset[int],
        body: bytes | None = None,
    ) -> None:
        if status in also_ok:
            return
        error = self._status_error(status, method, label, body)
        if error is not None:
            raise error

    def _status_error(
        self, status: int, method: str, label: str, body: bytes | None = None
    ) -> EngineError | None:
        if 200 <= status < 300:
            return None
        env = self.environment_id
        message = f"{method} {label} returned HTTP {status}"
        match status:
            case 401 | 403:
                return PermissionDenied(env, message, status_code=status)
            case 400:
                # Engine text may echo the request: scrub it before display; never log or record it.
                engine_message = _engine_message(body) if body else None
                return EngineError(env, message, status_code=status, engine_message=engine_message)
            case 404:
                engine_message = _engine_message(body) if body else None
                return NotFound(env, message, status_code=status, engine_message=engine_message)
            case 409:
                # Statuses other than 400 and 404 carry no Engine text: a 409 names a second
                # resource and the rest can echo credentials or registry detail.
                return Conflict(env, message, status_code=status)
            case _ if status in _GATEWAY_STATUSES:
                return EnvironmentUnreachable(env, message, status_code=status)
            case _:
                return EngineError(env, message, status_code=status)
