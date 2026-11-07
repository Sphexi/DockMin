"""Staged connectivity test for one registry credential set, naming the stage that fails."""

import asyncio
import socket
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from pydantic import SecretStr

from app import netpolicy
from app.logging import get_logger
from app.netcheck import (
    Address,
    BoundedResolver,
    CertificateFault,
    FactValue,
    Resolver,
    StageOutcome,
    TlsFailure,
    classify_certificate_fault,
    classify_tls_failure,
    clean_text,
    connect_first,
    dns_failure_message,
    literal_address,
    peer_facts,
    seconds_text,
    tcp_failure_message,
    usable_addresses,
)
from app.netpolicy import ip_literal
from app.registry_client import wire
from app.registry_client.addresses import Challenge, api_base_url
from app.registry_client.auth_config import basic_token, split_basic_token
from app.registry_client.ecr import DerivationRequest, DerivedSecretSource
from app.registry_client.errors import ChallengeInvalid, TokenExchangeFailed
from app.registry_client.providers import PROVIDERS, ProviderExtras, ProviderKind
from app.registry_client.tls import SystemTrustUnavailable, registry_context
from app.registry_client.token_flow import (
    Endpoint,
    Realm,
    challenges_of,
    choose_challenge,
    realm_of,
    token_from,
)

_log = get_logger(__name__)

_VERSION_HEADER = "docker-distribution-api-version"
_VERSION_VALUE = "registry/2.0"


class RegistryStage(StrEnum):
    """One step of the connectivity test; the values are stable identifiers."""

    CREDENTIAL_EXCHANGE = "credential_exchange"
    DNS = "dns"
    TCP = "tcp"
    TLS_HANDSHAKE = "tls_handshake"
    CERTIFICATE = "certificate"
    REGISTRY_API = "registry_api"
    AUTHENTICATION = "authentication"


_STAGE_SUBJECTS: Final = {
    RegistryStage.CREDENTIAL_EXCHANGE: "The credential exchange",
    RegistryStage.DNS: "The name lookup",
    RegistryStage.TCP: "The connection attempt",
    RegistryStage.TLS_HANDSHAKE: "The TLS handshake",
    RegistryStage.CERTIFICATE: "The certificate check",
    RegistryStage.REGISTRY_API: "The registry API request",
    RegistryStage.AUTHENTICATION: "The authentication check",
}


@dataclass(frozen=True, slots=True)
class RegistryProbeTimeouts:
    """Seconds each stage may take, and the cap on the whole test."""

    credential_exchange: float = 10.0
    dns: float = 5.0
    tcp: float = 5.0
    tls_handshake: float = 10.0
    registry_api: float = 10.0
    authentication: float = 10.0
    total: float = 30.0


@dataclass(frozen=True, slots=True)
class RegistryTarget:
    """What to test: the saved set's connection settings, never its secret."""

    provider: ProviderKind
    address: str
    verify_tls: bool
    ca_bundle: str | None = field(repr=False)
    extras: ProviderExtras


@dataclass(frozen=True, slots=True)
class RegistryLogin:
    """A user name and secret; neither appears in a repr."""

    username: str = field(repr=False)
    secret: SecretStr = field(repr=False)


@dataclass(frozen=True, slots=True)
class RegistryStageResult:
    """The outcome of one stage; `message` is product-written and `facts` hold bounded values."""

    stage: RegistryStage
    outcome: StageOutcome
    duration_ms: int
    message: str
    facts: Mapping[str, FactValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RegistryProbeResult:
    """The whole test: every stage that applies, in order."""

    stages: tuple[RegistryStageResult, ...]
    passed: bool
    failed_stage: RegistryStage | None
    message: str
    duration_ms: int


class RegistryProbeCoordinator:
    """Runs connectivity tests, coalescing concurrent tests of the same connection onto one."""

    def __init__(
        self,
        *,
        derivers: Mapping[str, DerivedSecretSource],
        resolver: Resolver | None = None,
        timeouts: RegistryProbeTimeouts | None = None,
    ) -> None:
        self._derivers = derivers
        self._owned: BoundedResolver | None
        if resolver is None:
            self._owned = BoundedResolver()
            self._resolver: Resolver = self._owned
        else:
            self._owned = None
            self._resolver = resolver
        self._timeouts = timeouts or RegistryProbeTimeouts()
        self._flights: dict[tuple[int, str], asyncio.Task[RegistryProbeResult]] = {}
        self._closed = False

    async def probe(
        self,
        target: RegistryTarget,
        login: RegistryLogin | None,
        *,
        key: tuple[int, str] | None,
    ) -> RegistryProbeResult:
        """Test the connection; callers passing the same key share one run.

        A key of None means the connection belongs to no saved set, so the run is never shared.
        Raises ValueError if credentials are supplied with verification off, or are missing for
        a provider that cannot be anonymous.
        """
        if self._closed:
            raise RuntimeError("the probe coordinator is closed")
        if login is not None and not target.verify_tls:
            raise ValueError("credentials are never sent without certificate verification")
        spec = PROVIDERS[target.provider]
        if login is None and not spec.anonymous_allowed:
            raise ValueError("this provider needs credentials")
        if key is None:
            return await self._run_test(target, login)
        flight = self._flights.get(key)
        if flight is None:
            flight = asyncio.create_task(self._run_test(target, login))
            self._flights[key] = flight
            flight.add_done_callback(lambda done: self._forget(key, done))
        return await asyncio.shield(flight)

    async def aclose(self) -> None:
        """Cancel tests still running; call once at shutdown."""
        self._closed = True
        flights = list(self._flights.values())
        for flight in flights:
            flight.cancel()
        await asyncio.gather(*flights, return_exceptions=True)
        self._flights.clear()
        if self._owned is not None:
            self._owned.close()

    def _forget(self, key: tuple[int, str], done: asyncio.Task[RegistryProbeResult]) -> None:
        if self._flights.get(key) is done:
            del self._flights[key]

    async def _run_test(
        self, target: RegistryTarget, login: RegistryLogin | None
    ) -> RegistryProbeResult:
        derived = PROVIDERS[target.provider].secret_kind == "derived"
        run = _Run(self._timeouts, derived, self._resolver, self._derivers)
        try:
            await run.run_stages(target, login)
        except Exception as error:
            _log.error(
                "registry_test_crashed",
                extra={"error": type(error).__name__},
            )
            run.crashed()
        return run.finish()


class _NetFailure(Exception):
    """A connection that could not be made; `message` is product-written."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class _Verdict:
    outcome: StageOutcome
    message: str
    facts: Mapping[str, FactValue] = field(default_factory=dict)


class _Run:
    def __init__(
        self,
        timeouts: RegistryProbeTimeouts,
        derived: bool,
        resolver: Resolver,
        derivers: Mapping[str, DerivedSecretSource],
    ) -> None:
        self._timeouts = timeouts
        self._derived = derived
        self._resolver = resolver
        self._derivers = derivers
        self._results: list[RegistryStageResult] = []
        self._started = time.monotonic()
        self._deadline = asyncio.get_running_loop().time() + timeouts.total
        self._order: tuple[RegistryStage, ...] = (
            *((RegistryStage.CREDENTIAL_EXCHANGE,) if derived else ()),
            RegistryStage.DNS,
            RegistryStage.TCP,
            RegistryStage.TLS_HANDSHAKE,
            RegistryStage.CERTIFICATE,
            RegistryStage.REGISTRY_API,
            RegistryStage.AUTHENTICATION,
        )

    async def run_stages(self, target: RegistryTarget, login: RegistryLogin | None) -> None:
        credentials: tuple[str, SecretStr] | None = None
        if login is not None:
            credentials = (login.username, login.secret)
        if self._derived:
            if login is None:
                raise ValueError("a derived credential needs a login")
            credentials = await self._exchange(target, login)
            if credentials is None:
                return

        base = urlsplit(api_base_url(target.provider, target.address))
        host = base.hostname or ""
        port = base.port or 443
        addresses = await self._dns(host, port)
        if addresses is None:
            return
        connected = await self._tcp(port, addresses)
        if connected is None:
            return
        try:
            endpoint = Endpoint(host, port, connected.family, connected.getpeername())
            context = await self._context(target)
            if context is None or not await self._tls(target, host, connected, context):
                return
        finally:
            connected.close()
        reply = await self._registry_api(endpoint, context)
        if reply is None:
            return
        await self._authentication(endpoint, context, reply, credentials)

    def finish(self) -> RegistryProbeResult:
        by_stage = {result.stage: result for result in self._results}
        stages = tuple(
            by_stage.get(stage) or RegistryStageResult(stage, StageOutcome.NOT_RUN, 0, "Not run.")
            for stage in self._order
        )
        failed = next((s for s in stages if s.outcome is StageOutcome.FAILED), None)
        duration = int((time.monotonic() - self._started) * 1000)
        message = failed.message if failed is not None else self._success_message(stages)
        return RegistryProbeResult(
            stages=stages,
            passed=failed is None,
            failed_stage=failed.stage if failed else None,
            message=message,
            duration_ms=duration,
        )

    def crashed(self) -> None:
        done = {result.stage for result in self._results}
        pending = next((stage for stage in self._order if stage not in done), None)
        if pending is not None:
            self._add(
                pending,
                StageOutcome.FAILED,
                "The connection test could not be completed.",
                time.monotonic(),
            )

    @staticmethod
    def _success_message(stages: tuple[RegistryStageResult, ...]) -> str:
        outcomes = {s.stage: s for s in stages}
        authentication = outcomes[RegistryStage.AUTHENTICATION]
        if authentication.outcome is StageOutcome.PASSED:
            return "The registry is reachable and accepted the credentials."
        return f"The registry is reachable. {authentication.message}"

    def _budget(self, limit: float) -> tuple[float, bool]:
        remaining = self._deadline - asyncio.get_running_loop().time()
        return max(0.0, min(limit, remaining)), remaining < limit

    def _add(
        self,
        stage: RegistryStage,
        outcome: StageOutcome,
        message: str,
        started: float,
        facts: Mapping[str, FactValue] | None = None,
    ) -> None:
        self._results.append(
            RegistryStageResult(
                stage, outcome, int((time.monotonic() - started) * 1000), message, facts or {}
            )
        )

    def _fail(
        self,
        stage: RegistryStage,
        message: str,
        started: float,
        error: BaseException | None = None,
    ) -> None:
        if error is not None:
            _log.debug(
                "registry test stage failed",
                extra={"stage": stage.value, "error_class": type(error).__name__},
            )
        self._add(stage, StageOutcome.FAILED, message, started)

    def _timed_out(
        self, stage: RegistryStage, seconds: float, overall: bool, started: float
    ) -> None:
        if overall:
            message = (
                f"The test reached its overall time limit of {seconds_text(self._timeouts.total)}."
            )
        else:
            message = f"{_STAGE_SUBJECTS[stage]} did not finish within {seconds_text(seconds)}."
        self._fail(stage, message, started)

    async def _exchange(
        self, target: RegistryTarget, login: RegistryLogin
    ) -> tuple[str, SecretStr] | None:
        stage = RegistryStage.CREDENTIAL_EXCHANGE
        started = time.monotonic()
        deriver = self._derivers.get(target.provider)
        if deriver is None:
            self._fail(stage, "No credential exchange is configured for this provider.", started)
            return None
        seconds, overall = self._budget(self._timeouts.credential_exchange)
        request = DerivationRequest(
            provider=target.provider,
            address=target.address,
            username=login.username,
            secret=login.secret,
            extras=target.extras,
        )
        try:
            async with asyncio.timeout(seconds):
                derived = await deriver.derive(request)
            credentials = split_basic_token(derived.basic_token)
        except TimeoutError:
            self._timed_out(stage, seconds, overall, started)
            return None
        except TokenExchangeFailed as exc:
            self._fail(stage, exc.message, started, exc)
            return None
        except Exception as exc:
            self._fail(stage, "The credential exchange could not be completed.", started, exc)
            return None
        self._add(stage, StageOutcome.PASSED, "The credential exchange issued a token.", started)
        return credentials

    async def _dns(self, host: str, port: int) -> list[Address] | None:
        stage = RegistryStage.DNS
        started = time.monotonic()
        literal = ip_literal(host)
        if literal is not None:
            self._add(
                stage,
                StageOutcome.SKIPPED,
                "The host is an IP address, so no name lookup is needed.",
                started,
            )
            return [literal_address(literal, port)]
        seconds, overall = self._budget(self._timeouts.dns)
        try:
            async with asyncio.timeout(seconds):
                infos = await self._resolver(host, port)
        except TimeoutError:
            self._timed_out(stage, seconds, overall, started)
            return None
        except socket.gaierror as exc:
            self._fail(stage, dns_failure_message(exc.errno), started, exc)
            return None
        except Exception as exc:
            self._fail(stage, "The name lookup failed.", started, exc)
            return None
        addresses = usable_addresses(infos)
        if not addresses:
            self._fail(stage, "The name resolved, but to no usable address.", started)
            return None
        self._add(
            stage,
            StageOutcome.PASSED,
            f"The name resolved to {len(addresses)} address{'' if len(addresses) == 1 else 'es'}.",
            started,
            {"addresses": len(addresses)},
        )
        return addresses

    async def _tcp(self, port: int, addresses: list[Address]) -> socket.socket | None:
        started = time.monotonic()

        def log_failure(exc: OSError) -> None:
            _log.debug(
                "registry test stage failed",
                extra={"stage": RegistryStage.TCP.value, "error_class": type(exc).__name__},
            )

        result = await connect_first(
            addresses,
            attempt_limit=self._timeouts.tcp,
            budget=self._budget,
            observe=log_failure,
        )
        if result.sock is None:
            self._fail(
                RegistryStage.TCP,
                tcp_failure_message(
                    result.kinds,
                    tcp_timeout=self._timeouts.tcp,
                    total_timeout=self._timeouts.total,
                ),
                started,
            )
            return None
        self._add(
            RegistryStage.TCP,
            StageOutcome.PASSED,
            f"Connected to {result.sockaddr[0]} port {port}.",
            started,
            {"address": str(result.sockaddr[0]), "attempts": result.attempts},
        )
        return result.sock

    async def _context(self, target: RegistryTarget) -> ssl.SSLContext | None:
        started = time.monotonic()
        try:
            return await asyncio.to_thread(
                registry_context, verify_tls=target.verify_tls, ca_bundle=target.ca_bundle
            )
        except SystemTrustUnavailable as exc:
            self._fail(
                RegistryStage.TLS_HANDSHAKE,
                "This server has no operating system root certificates to verify the registry "
                "with. Install them in the image this server runs from.",
                started,
                exc,
            )
            return None
        except (ssl.SSLError, ValueError) as exc:
            self._fail(
                RegistryStage.TLS_HANDSHAKE,
                "The stored CA bundle could not be loaded. Replace it.",
                started,
                exc,
            )
            return None

    async def _tls(
        self,
        target: RegistryTarget,
        host: str,
        connected: socket.socket,
        context: ssl.SSLContext,
    ) -> bool:
        started = time.monotonic()
        seconds, overall = self._budget(self._timeouts.tls_handshake)
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(seconds):
                _, writer = await asyncio.open_connection(
                    sock=connected, ssl=context, server_hostname=host, limit=wire.MAX_HEAD_BYTES
                )
                peer = peer_facts(writer.get_extra_info("ssl_object"))
        except TimeoutError:
            self._timed_out(RegistryStage.TLS_HANDSHAKE, seconds, overall, started)
            return False
        except ssl.SSLCertVerificationError as exc:
            _log.debug(
                "registry test stage failed",
                extra={"stage": RegistryStage.CERTIFICATE.value, "error_class": type(exc).__name__},
            )
            self._add(
                RegistryStage.TLS_HANDSHAKE,
                StageOutcome.PASSED,
                "The server speaks TLS and a protocol version was agreed.",
                started,
            )
            self._add(
                RegistryStage.CERTIFICATE,
                StageOutcome.FAILED,
                _certificate_message(classify_certificate_fault(exc.verify_code), "registry"),
                started,
            )
            return False
        except ssl.SSLError as exc:
            self._fail(RegistryStage.TLS_HANDSHAKE, _tls_message(exc), started, exc)
            return False
        except (ConnectionError, EOFError) as exc:
            self._fail(RegistryStage.TLS_HANDSHAKE, _tls_message(None), started, exc)
            return False
        except OSError as exc:
            self._fail(RegistryStage.TLS_HANDSHAKE, "The TLS connection failed.", started, exc)
            return False
        finally:
            if writer is not None:
                writer.transport.abort()

        self._add(
            RegistryStage.TLS_HANDSHAKE,
            StageOutcome.PASSED,
            "The TLS handshake completed.",
            started,
            {"tls_version": peer.version},
        )
        if target.verify_tls:
            self._add(
                RegistryStage.CERTIFICATE,
                StageOutcome.PASSED,
                "The registry's certificate is trusted and covers the address.",
                started,
                {"expires": peer.expires} if peer.expires else {},
            )
        else:
            self._add(
                RegistryStage.CERTIFICATE,
                StageOutcome.SKIPPED,
                "The certificate was not checked: verification is turned off for this set.",
                started,
            )
        return True

    async def _registry_api(
        self, endpoint: Endpoint, context: ssl.SSLContext
    ) -> wire.WireResponse | None:
        stage = RegistryStage.REGISTRY_API
        started = time.monotonic()
        seconds, overall = self._budget(self._timeouts.registry_api)
        try:
            async with asyncio.timeout(seconds):
                reply = await _fetch(endpoint, context, "/v2/", {}, "registry")
        except TimeoutError:
            self._timed_out(stage, seconds, overall, started)
            return None
        except _NetFailure as exc:
            self._fail(stage, exc.message, started, exc)
            return None
        problem = _registry_api_problem(reply)
        if problem is not None:
            self._fail(stage, problem, started)
            return None
        self._add(
            stage,
            StageOutcome.PASSED,
            f"The registry answered the v2 API with HTTP {reply.status}.",
            started,
            {"http_status": reply.status},
        )
        return reply

    async def _authentication(
        self,
        endpoint: Endpoint,
        context: ssl.SSLContext,
        api_reply: wire.WireResponse,
        credentials: tuple[str, SecretStr] | None,
    ) -> None:
        stage = RegistryStage.AUTHENTICATION
        started = time.monotonic()
        seconds, overall = self._budget(self._timeouts.authentication)
        try:
            async with asyncio.timeout(seconds):
                verdict = await self._authenticate(endpoint, context, api_reply, credentials)
        except TimeoutError:
            self._timed_out(stage, seconds, overall, started)
            return
        except _NetFailure as exc:
            self._fail(stage, exc.message, started, exc)
            return
        self._add(stage, verdict.outcome, verdict.message, started, verdict.facts)

    async def _authenticate(
        self,
        endpoint: Endpoint,
        context: ssl.SSLContext,
        api_reply: wire.WireResponse,
        credentials: tuple[str, SecretStr] | None,
    ) -> _Verdict:
        if credentials is None:
            return _Verdict(
                StageOutcome.SKIPPED,
                "Anonymous access is decided per repository at pull time, so no credentials "
                "were tested.",
            )
        if api_reply.status == 200:
            return _Verdict(StageOutcome.SKIPPED, "The registry did not ask for credentials.")
        challenge = choose_challenge(api_reply)
        if isinstance(challenge, str):
            return _failed(challenge)
        try:
            authorization = "Basic " + basic_token(*credentials).get_secret_value()
        except ValueError:
            return _failed("The user name cannot be sent with Basic authentication.")
        if challenge.scheme == "basic":
            reply = await _fetch(
                endpoint, context, "/v2/", {"Authorization": authorization}, "registry"
            )
            return _verdict_for("registry", reply, {"method": "basic"})
        return await self._bearer(endpoint, context, challenge, authorization)

    async def _bearer(
        self,
        endpoint: Endpoint,
        context: ssl.SSLContext,
        challenge: Challenge,
        authorization: str,
    ) -> _Verdict:
        realm = realm_of(challenge)
        if isinstance(realm, str):
            return _failed(realm)
        facts: dict[str, FactValue] = {"method": "bearer", "token_service": clean_text(realm.host)}
        service_endpoint = await self._token_endpoint(endpoint, realm)
        token_reply = await _fetch(
            service_endpoint,
            context,
            realm.target,
            {"Authorization": authorization},
            "token service",
            max_body=wire.MAX_BODY_BYTES,
        )
        if token_reply.status != 200:
            return _verdict_for("token service", token_reply, facts)
        token = token_from(token_reply.body)
        if token is None:
            return _failed("The token service's reply held no usable token.", facts)
        reply = await _fetch(
            endpoint, context, "/v2/", {"Authorization": f"Bearer {token}"}, "registry"
        )
        if reply.status == 200:
            return _Verdict(
                StageOutcome.PASSED,
                "The token service issued a token and the registry accepted it.",
                facts,
            )
        if reply.status in (401, 403):
            return _failed("The registry rejected the token the token service issued.", facts)
        return _failed(f"The registry answered HTTP {reply.status} to the token.", facts)

    async def _token_endpoint(self, endpoint: Endpoint, realm: Realm) -> Endpoint:
        if (realm.host, realm.port) == (endpoint.host, endpoint.port):
            return endpoint
        literal = ip_literal(realm.host)
        if literal is not None:
            addresses = [literal_address(literal, realm.port)]
        else:
            addresses = await self._resolve_for_token(realm)
        seconds, _ = self._budget(self._timeouts.tcp)
        result = await connect_first(
            addresses,
            attempt_limit=self._timeouts.tcp,
            budget=lambda limit: (min(limit, seconds), False),
        )
        if result.sock is None:
            raise _NetFailure(
                "The token service could not be reached: "
                + tcp_failure_message(
                    result.kinds,
                    tcp_timeout=self._timeouts.tcp,
                    total_timeout=self._timeouts.total,
                )
            )
        family = result.sock.family
        result.sock.close()
        return Endpoint(realm.host, realm.port, family, result.sockaddr)

    async def _resolve_for_token(self, realm: Realm) -> list[Address]:
        problem: str | None = None
        addresses: list[Address] = []
        try:
            addresses = usable_addresses(await self._resolver(realm.host, realm.port))
        except socket.gaierror as exc:
            reason = dns_failure_message(exc.errno)
            problem = f"The token service name could not be resolved: {reason}"
        except Exception:
            problem = "The token service name could not be resolved."
        if problem is None and not addresses:
            problem = "The token service name resolved, but to no usable address."
        if problem is not None:
            raise _NetFailure(problem)
        return addresses


def _failed(message: str, facts: Mapping[str, FactValue] | None = None) -> _Verdict:
    return _Verdict(StageOutcome.FAILED, message, facts or {})


def _verdict_for(
    subject: str, reply: wire.WireResponse, facts: Mapping[str, FactValue]
) -> _Verdict:
    if reply.status == 200:
        return _Verdict(StageOutcome.PASSED, "The registry accepted the credentials.", facts)
    if reply.status in (401, 403):
        return _failed(f"The {subject} rejected the credentials.", facts)
    return _failed(f"The {subject} answered HTTP {reply.status} to the credentials.", facts)


def _registry_api_problem(reply: wire.WireResponse) -> str | None:
    if 300 <= reply.status < 400:
        return (
            f"The address answered with a redirect (HTTP {reply.status}). Redirects are not "
            "followed; use the address the registry is served from."
        )
    if reply.status not in (200, 401):
        return (
            f"The address answered HTTP {reply.status}, which is not a Docker registry API "
            "response."
        )
    version = (reply.header(_VERSION_HEADER) or "").strip().lower()
    if version == _VERSION_VALUE:
        return None
    if reply.status == 401:
        try:
            challenges = challenges_of(reply)
        except ChallengeInvalid:
            challenges = ()
        if any(challenge.scheme in ("basic", "bearer") for challenge in challenges):
            return None
    return "The address answered, but not like a Docker registry: it sent no registry API header."


async def _fetch(
    endpoint: Endpoint,
    context: ssl.SSLContext,
    target: str,
    headers: Mapping[str, str],
    subject: str,
    *,
    max_body: int | None = None,
) -> wire.WireResponse:
    """Make one request on a new connection to the pinned address; raise _NetFailure on trouble."""
    failure: _NetFailure | None = None
    writer: asyncio.StreamWriter | None = None
    reply: wire.WireResponse | None = None
    try:
        reader, writer = await wire.open_connection(
            endpoint.family, endpoint.sockaddr, server_name=endpoint.host, context=context
        )
        reply = await wire.get(
            reader,
            writer,
            target=target,
            host_header=endpoint.host_header,
            headers=headers,
            max_body=max_body,
        )
    except netpolicy.AddressRefused as exc:
        failure = _NetFailure(str(exc))
    except wire.WireError as exc:
        failure = _NetFailure(_wire_message(exc.failure, subject))
    except ssl.SSLCertVerificationError as exc:
        failure = _NetFailure(
            _certificate_message(classify_certificate_fault(exc.verify_code), subject)
        )
    except ssl.SSLError as exc:
        failure = _NetFailure(f"The TLS connection to the {subject} failed: {_tls_message(exc)}")
    except (OSError, EOFError):
        failure = _NetFailure(f"The connection to the {subject} was lost.")
    finally:
        if writer is not None:
            writer.transport.abort()
    if failure is not None:
        raise failure
    assert reply is not None
    return reply


def _wire_message(failure: wire.WireFailure, subject: str) -> str:
    match failure:
        case wire.WireFailure.CLOSED:
            return f"The {subject} closed the connection without answering."
        case wire.WireFailure.OVERSIZED:
            return f"The {subject}'s reply was far larger than a registry sends."
        case _:
            return f"The {subject}'s reply was not valid HTTP."


def _tls_message(exc: ssl.SSLError | None) -> str:
    failure = classify_tls_failure(exc) if exc is not None else TlsFailure.CLOSED
    match failure:
        case TlsFailure.CLOSED:
            return (
                "The server closed the connection during the TLS handshake. It may not support "
                "TLS 1.2 or later."
            )
        case TlsFailure.NOT_TLS:
            return (
                "The server does not speak TLS on this port. Registries are served over https; "
                "check the port."
            )
        case TlsFailure.PROTOCOL_VERSION:
            return (
                "The server does not offer TLS 1.2 or later, which is the minimum this product "
                "uses."
            )
        case TlsFailure.HANDSHAKE_REJECTED:
            return "The server rejected the handshake; it shares no cipher suite with this server."
        case TlsFailure.CERTIFICATE_ALERT | TlsFailure.ALERT:
            return "The server ended the handshake with a TLS alert."
        case _:
            return "The TLS handshake failed."


def _certificate_message(fault: CertificateFault, subject: str) -> str:
    match fault:
        case CertificateFault.UNKNOWN_ISSUER:
            return (
                f"The {subject}'s certificate was not issued by a trusted authority. If it uses "
                "a private CA, add that CA certificate to this set's CA bundle."
            )
        case CertificateFault.EXPIRED:
            return f"The {subject}'s certificate has expired."
        case CertificateFault.NOT_YET_VALID:
            return (
                f"The {subject}'s certificate is not valid yet. Check the clocks on its host "
                "and on this server."
            )
        case CertificateFault.NAME_MISMATCH:
            return (
                f"The {subject}'s certificate does not cover the host name or IP address used "
                "to reach it."
            )
        case CertificateFault.WRONG_PURPOSE:
            return f"The {subject}'s certificate is not valid for server authentication."
        case CertificateFault.WEAK_KEY:
            return (
                f"The {subject}'s certificate, or its issuer, uses a key or digest that is too "
                "weak."
            )
        case _:
            return f"The {subject}'s certificate could not be verified."


__all__: Final = [
    "RegistryLogin",
    "RegistryProbeCoordinator",
    "RegistryProbeResult",
    "RegistryProbeTimeouts",
    "RegistryStage",
    "RegistryStageResult",
    "RegistryTarget",
]
