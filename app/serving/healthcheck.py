"""Container health probe: python -m app.serving.healthcheck. Standard library only."""

import http.client
import os
import ssl
import sys
from collections.abc import Mapping
from typing import Final

from app.paths import HEALTH_PATH

TIMEOUT_SECONDS: Final = 4.0
DEFAULT_PORT: Final = "8000"
DEFAULT_HTTPS_PORT: Final = "8443"
LOOPBACK_FOR_WILDCARD: Final = {"0.0.0.0": "127.0.0.1", "::": "::1"}
FALSE_WORDS: Final = frozenset({"0", "false", "f", "no", "n", "off"})


def main() -> int:
    """Probe the listener the process serves with; return 0 when it reports healthy."""
    try:
        host, port, secure = _target(os.environ)
        status = _probe(host, port, secure)
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(f"health probe failed: {type(error).__name__}", file=sys.stderr)
        return 1
    if status != http.HTTPStatus.OK:
        print(f"health probe failed: status {status}", file=sys.stderr)
        return 1
    return 0


def _target(raw_environ: Mapping[str, str]) -> tuple[str, int, bool]:
    environ = {name.upper(): value for name, value in raw_environ.items()}
    supplied = bool(environ.get("DOCKMIN_TLS_CERT_FILE", "").strip()) and bool(
        environ.get("DOCKMIN_TLS_KEY_FILE", "").strip()
    )
    self_signed = environ.get("DOCKMIN_TLS_SELF_SIGNED", "").strip().lower() not in FALSE_WORDS
    secure = supplied or self_signed
    port = environ.get("DOCKMIN_HTTPS_PORT" if secure else "DOCKMIN_PORT", "").strip()
    host = environ.get("DOCKMIN_HOST", "").strip() or "0.0.0.0"
    return (
        LOOPBACK_FOR_WILDCARD.get(host, host),
        int(port or (DEFAULT_HTTPS_PORT if secure else DEFAULT_PORT)),
        secure,
    )


def _probe(host: str, port: int, secure: bool) -> int:
    connection: http.client.HTTPConnection
    if secure:
        # The probe dials its own listener by loopback address; a generated certificate has no chain
        # to verify and a supplied one names the public host.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            host, port, timeout=TIMEOUT_SECONDS, context=context
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=TIMEOUT_SECONDS)
    try:
        connection.request("GET", HEALTH_PATH)
        return connection.getresponse().status
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
