"""Launcher: loads settings, checks TLS material, and serves one or two listeners in one process."""

import argparse
import asyncio
import contextlib
import signal
import socket
import ssl
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import TLS_SELF_SIGNED_VAR, ConfigError, Settings, load_settings
from app.logging import AccessLogMiddleware, configure_logging, get_logger
from app.main import create_app
from app.selfcheck import (
    TLS_REMEDY,
    StartupCheckFailed,
    check_data_directory,
    from_config_error,
    log_check_failure,
)
from app.serving.redirect import REDIRECT_LISTENER, HttpsRedirectApp
from app.tls import (
    TLS_LISTENER_SCOPE_KEY,
    TLS_SELF_SIGNED_SCOPE_KEY,
    CertificateSummary,
    CertificateUnusable,
    expiry_state,
    load_server_tls,
)
from app.tls_self_signed import SelfSignedPair, ensure_self_signed

EXIT_OK: Final = 0
EXIT_CONFIGURATION: Final = 2
EXIT_STARTUP_CHECK: Final = 3
WS_MAX_SIZE: Final = 65_536
GRACEFUL_SHUTDOWN_SECONDS: Final = 10
STARTUP_POLL_SECONDS: Final = 0.05
WILDCARD_PROXY: Final = "*"
SOCKET_STALL_TIMEOUT_MS: Final = 120_000

logger = get_logger(__name__)


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the launcher and bounds stalled sends."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            for server in self.servers:
                for listener in server.sockets:
                    listener.setsockopt(
                        socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, SOCKET_STALL_TIMEOUT_MS
                    )


class _TlsListener:
    """Mark every connection on the HTTPS listener, so the mark cannot be set by a client."""

    def __init__(self, app: ASGIApp, *, self_signed: bool) -> None:
        self._app = app
        self._self_signed = self_signed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope[TLS_LISTENER_SCOPE_KEY] = True
        if self._self_signed:
            scope[TLS_SELF_SIGNED_SCOPE_KEY] = True
        await self._app(scope, receive, send)


def server_configs(
    settings: Settings, app: ASGIApp, tls_context: ssl.SSLContext | None = None
) -> list[uvicorn.Config]:
    """Describe the listeners, primary first; the primary owns the application lifespan.

    `tls_context` is required when TLS is enabled.
    """
    if not settings.tls_enabled:
        return [_config(settings, app, port=settings.port)]
    assert tls_context is not None
    configs = [
        _config(
            settings,
            _TlsListener(app, self_signed=settings.tls_source == "self_signed"),
            port=settings.https_port,
            ssl_context_factory=lambda config, default: tls_context,
        )
    ]
    match settings.effective_http_mode:
        case "serve":
            configs.append(_config(settings, app, port=settings.port, lifespan="off"))
        case "redirect":
            redirect: ASGIApp = HttpsRedirectApp(
                public_port=settings.redirect_port, trusted_origins=settings.trusted_origins
            )
            if settings.access_log:
                redirect = AccessLogMiddleware(
                    redirect, listener=REDIRECT_LISTENER, refusals_only=True
                )
            configs.append(
                _config(settings, redirect, port=settings.port, lifespan="off", proxy_headers=False)
            )
    return configs


def _config(settings: Settings, app: ASGIApp, *, port: int, **overrides: Any) -> uvicorn.Config:
    # log_level stays unset: uvicorn would otherwise reset the levels configure_logging pins on
    # the loggers that carry websocket frame contents.
    options: dict[str, Any] = {
        "proxy_headers": bool(settings.trusted_proxies),
        "forwarded_allow_ips": _forwarded_allow_ips(settings),
        **overrides,
    }
    return uvicorn.Config(
        app,
        host=settings.host,
        port=port,
        ws_max_size=WS_MAX_SIZE,
        log_config=None,
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        **options,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped; return 0, 2 for a configuration error, 3 for a failed startup check."""
    argparse.ArgumentParser(
        prog="python -m app.serving", description="Serve dockmin on the configured listeners."
    ).parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as error:
        return _report_configuration_error(error)
    configure_logging(settings.log_level)
    try:
        tls, generated = _load_tls(settings)
    except StartupCheckFailed as failure:
        log_check_failure(failure)
        return EXIT_STARTUP_CHECK
    certificate = None if tls is None else tls[1]
    if certificate is not None:
        _log_certificate_state(certificate, datetime.now(UTC))
        if generated is not None:
            _log_self_signed(generated, certificate)
    app = create_app(settings)
    app.state.served_certificate = certificate
    configs = server_configs(settings, app, None if tls is None else tls[0])
    return asyncio.run(_supervise(configs), loop_factory=configs[0].get_loop_factory())


def _report_configuration_error(error: ConfigError) -> int:
    configure_logging()
    failure = from_config_error(error)
    if failure is None:
        logger.error("configuration_invalid", extra={"problem": str(error)})
    else:
        log_check_failure(failure)
    return EXIT_CONFIGURATION


def _load_tls(
    settings: Settings,
) -> tuple[tuple[ssl.SSLContext, CertificateSummary] | None, SelfSignedPair | None]:
    cert_file, key_file = settings.served_cert_file, settings.served_key_file
    if cert_file is None or key_file is None:
        return None, None
    try:
        if settings.tls_source == "self_signed":
            check_data_directory(settings.data_dir)
            generated = ensure_self_signed(cert_file.parent, datetime.now(UTC))
            return generated.tls, generated
        return load_server_tls(cert_file, key_file), None
    except CertificateUnusable as error:
        remedy = (
            _self_signed_remedy(settings.data_dir)
            if settings.tls_source == "self_signed"
            else TLS_REMEDY
        )
        raise StartupCheckFailed("tls_material", str(error), remedy) from None


def _self_signed_remedy(data_dir: Path) -> str:
    return (
        f"Make {data_dir} writable by the user the process runs as (uid 10001 in the image), or "
        f"set {TLS_SELF_SIGNED_VAR}=false and supply a certificate or terminate TLS in front of "
        "dockmin"
    )


def _forwarded_allow_ips(settings: Settings) -> list[str] | str:
    if WILDCARD_PROXY in settings.trusted_proxies:
        return WILDCARD_PROXY
    return list(settings.trusted_proxies)


def _log_certificate_state(certificate: CertificateSummary, now: datetime) -> None:
    state = expiry_state(certificate, now)
    fields = {
        "subject": certificate.subject,
        "not_before": certificate.not_before.isoformat(),
        "not_after": certificate.not_after.isoformat(),
    }
    if state in ("expired", "not_yet_valid"):
        logger.warning("tls_certificate_invalid_now", extra={"state": state, **fields})
    elif state == "expiring":
        days_left = (certificate.not_after - now).days
        logger.warning("tls_certificate_expiring", extra={"days_left": days_left, **fields})


def _log_self_signed(pair: SelfSignedPair, certificate: CertificateSummary) -> None:
    logger.info(
        "tls_certificate_self_signed",
        extra={
            "sha256_fingerprint": certificate.sha256_fingerprint,
            "not_after": certificate.not_after.isoformat(),
            "generated": pair.created,
            "reason": pair.reason,
        },
    )


async def _run(server: _Server) -> int:
    try:
        if not server.config.loaded:
            await asyncio.to_thread(server.config.load)
        await server.serve()
    except SystemExit as request:
        return EXIT_OK if request.code in (None, 0) else EXIT_STARTUP_CHECK
    return EXIT_OK


def _stop_accepting(server: uvicorn.Server) -> None:
    # `servers` exists only once uvicorn has bound its listening sockets.
    for listener in getattr(server, "servers", ()):
        listener.close()


async def _supervise(configs: Sequence[uvicorn.Config]) -> int:
    primary, *others = (_Server(config) for config in configs)
    secondary = others[0] if others else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(number: int) -> None:
        if stop.is_set() and number == signal.SIGINT:
            primary.force_exit = True
            if secondary is not None:
                secondary.force_exit = True
        stop.set()

    handled = (signal.SIGTERM, signal.SIGINT)
    for number in handled:
        loop.add_signal_handler(number, request_stop, number)
    primary_task = asyncio.create_task(_run(primary))
    secondary_task: asyncio.Task[int] | None = None
    try:
        # uvicorn reports a finished startup only through the started flag.
        while not (primary.started or primary_task.done() or stop.is_set()):  # noqa: ASYNC110
            await asyncio.sleep(STARTUP_POLL_SECONDS)
        if secondary is not None and primary.started and not stop.is_set():
            secondary_task = asyncio.create_task(_run(secondary))
        stopped = asyncio.create_task(stop.wait())
        waiting: set[asyncio.Task[Any]] = {primary_task, stopped}
        if secondary_task is not None:
            waiting.add(secondary_task)
        await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        stopped.cancel()
        statuses: list[int] = []
        try:
            if secondary is not None and secondary_task is not None:
                _stop_accepting(primary)
                secondary.should_exit = True
                statuses.append(await secondary_task)
        finally:
            primary.should_exit = True
            statuses.append(await primary_task)
    finally:
        for number in handled:
            loop.remove_signal_handler(number)
    return max(statuses)
