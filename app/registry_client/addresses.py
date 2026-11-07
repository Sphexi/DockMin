"""Registry address canonicalisation, endpoint derivation and WWW-Authenticate parsing."""

import ipaddress
import re
import string
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from app.registry_client.errors import AddressInvalid, ChallengeInvalid

MAX_ADDRESS_LENGTH: Final = 262
MAX_CHALLENGE_HEADER: Final = 4096
MAX_CHALLENGE_PARAMS: Final = 16
MAX_CHALLENGES: Final = 8
DEFAULT_PORT: Final = 443
DOCKER_HUB_ADDRESS: Final = "docker.io"
DOCKER_HUB_API_HOST: Final = "registry-1.docker.io"
DOCKER_HUB_SERVER_ADDRESS: Final = "https://index.docker.io/v1/"

_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_MAX_HOST_LENGTH = 253
_MAX_PORT_DIGITS = 5
_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "!#$%&'*+-.^_`|~")
_VALUE_CHARS = _TOKEN_CHARS | frozenset("/=")

_NOT_ALLOWED = "A registry address is a host name or IP address with an optional port"
_MESSAGES = {
    "empty": "Enter the registry's host name or IP address.",
    "long": "The registry address is too long.",
    "space": "The registry address must not contain spaces or control characters.",
    "http": (
        "Plain-HTTP registries are not supported. Credentials would cross the network "
        "unencrypted; use an https address."
    ),
    "scheme": f"{_NOT_ALLOWED}; only the https scheme may be written in front of it.",
    "path": f"{_NOT_ALLOWED}, without a path.",
    "userinfo": f"{_NOT_ALLOWED}, without a user name or password in it.",
    "query": f"{_NOT_ALLOWED}, without a query string.",
    "fragment": f"{_NOT_ALLOWED}, without a fragment.",
    "percent": f"{_NOT_ALLOWED}, without percent-encoded characters.",
    "backslash": f"{_NOT_ALLOWED}, without backslashes.",
    "ipv6": "An IPv6 address must be written in square brackets.",
    "bracket": "The IPv6 address in square brackets is not valid.",
    "port": "The port must be a number from 1 to 65535.",
    "host": "The host name is not valid.",
    "numeric": "The host looks like a number but is not a dotted IPv4 address.",
}


def _refuse(kind: str) -> AddressInvalid:
    return AddressInvalid(_MESSAGES[kind])


def canonical_address(text: str) -> str:
    """Return the stored form `host[:port]`, or raise AddressInvalid.

    The caller trims surrounding whitespace; any whitespace left here is refused.
    """
    if not text:
        raise _refuse("empty")
    if len(text) > MAX_ADDRESS_LENGTH * 2:
        raise _refuse("long")
    if any(c.isspace() or unicodedata.category(c) in _HIDDEN_CATEGORIES for c in text):
        raise _refuse("space")

    rest = _strip_scheme(text)
    if rest.endswith("/"):
        rest = rest[:-1]
    for marker, kind in (
        ("@", "userinfo"),
        ("?", "query"),
        ("#", "fragment"),
        ("%", "percent"),
        ("\\", "backslash"),
        ("/", "path"),
    ):
        if marker in rest:
            raise _refuse(kind)

    host, port = _split_host_port(rest)
    canonical = _canonical_host(host)
    if port != DEFAULT_PORT:
        canonical = f"{canonical}:{port}"
    if len(canonical) > MAX_ADDRESS_LENGTH:
        raise _refuse("long")
    return canonical


def split_address(address: str) -> tuple[str, int]:
    """Return the unbracketed host and the port of an address, validating it first."""
    canonical = canonical_address(address)
    host, port = _split_host_port(canonical)
    return host, port


def api_base_url(kind: str, address: str) -> str:
    """Return the origin the registry v2 API is served from."""
    if _is_docker_hub(kind, address):
        return f"https://{DOCKER_HUB_API_HOST}"
    return f"https://{address}"


def server_address(kind: str, address: str) -> str:
    """Return the `serveraddress` value of the Docker auth configuration."""
    if _is_docker_hub(kind, address):
        return DOCKER_HUB_SERVER_ADDRESS
    return address


def _is_docker_hub(kind: str, address: str) -> bool:
    return kind == "dockerhub" or address == DOCKER_HUB_ADDRESS


def _strip_scheme(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith("https://"):
        return text[len("https://") :]
    if lowered.startswith("http://"):
        raise _refuse("http")
    if "://" in text:
        raise _refuse("scheme")
    return text


def _split_host_port(text: str) -> tuple[str, int]:
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise _refuse("bracket")
        host, tail = text[1:end], text[end + 1 :]
        if tail and not tail.startswith(":"):
            raise _refuse("bracket")
        port_text = tail[1:] if tail else None
    elif text.count(":") > 1:
        raise _refuse("ipv6")
    else:
        host, separator, port_text = text.partition(":")
        if not separator:
            port_text = None
    if port_text is None:
        return host, DEFAULT_PORT
    if (
        not port_text
        or len(port_text) > _MAX_PORT_DIGITS
        or not (port_text.isascii() and port_text.isdecimal())
    ):
        raise _refuse("port")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise _refuse("port")
    return host, port


def _canonical_host(host: str) -> str:
    if not host:
        raise _refuse("empty")
    if ":" in host:
        try:
            return f"[{ipaddress.IPv6Address(host)}]"
        except ValueError:
            raise _refuse("bracket") from None
    if host.endswith("."):
        host = host[:-1]
    ascii_host = _to_ascii(host)
    if not ascii_host or len(ascii_host) > _MAX_HOST_LENGTH:
        raise _refuse("host")
    labels = ascii_host.split(".")
    if not all(_LABEL.match(label) for label in labels):
        raise _refuse("host")
    try:
        return str(ipaddress.IPv4Address(ascii_host))
    except ValueError:
        pass
    last = labels[-1]
    if last.isdecimal() or last.startswith("0x"):
        raise _refuse("numeric")
    return ascii_host


def _to_ascii(host: str) -> str:
    if host.isascii():
        return host.lower()
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise _refuse("host") from None


@dataclass(frozen=True, slots=True)
class Challenge:
    """One authentication challenge: a lower-cased scheme and its lower-cased parameter names."""

    scheme: str
    params: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def param(self, name: str) -> str | None:
        """Return a parameter value, or None."""
        return self.params.get(name)


def parse_challenges(header: str) -> tuple[Challenge, ...]:
    """Parse one WWW-Authenticate header value, or raise ChallengeInvalid.

    Input over 4 KiB, more than 16 parameters in total, more than 8 challenges, non-ASCII text and
    control characters are all refused.
    """
    if len(header) > MAX_CHALLENGE_HEADER:
        raise ChallengeInvalid("The authentication challenge is too large.")
    if not header.isascii() or any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in header):
        raise ChallengeInvalid("The authentication challenge is malformed.")
    return _ChallengeReader(header).read()


class _ChallengeReader:
    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._params = 0

    def read(self) -> tuple[Challenge, ...]:
        challenges: list[Challenge] = []
        while True:
            self._skip(" \t,")
            if self._pos >= len(self._text):
                return tuple(challenges)
            if len(challenges) >= MAX_CHALLENGES:
                raise _malformed()
            challenges.append(self._challenge())

    def _challenge(self) -> Challenge:
        scheme = self._token()
        params: dict[str, str] = {}
        first = True
        while True:
            self._skip(" \t")
            if self._pos >= len(self._text):
                break
            after_comma = self._text[self._pos] == ","
            if after_comma:
                self._skip(" \t,")
                if self._pos >= len(self._text):
                    break
            elif not first:
                raise _malformed()
            start = self._pos
            name = self._token()
            self._skip(" \t")
            if self._pos < len(self._text) and self._text[self._pos] == "=":
                self._pos += 1
                self._skip(" \t")
                params.setdefault(name.lower(), self._value())
                self._params += 1
                if self._params > MAX_CHALLENGE_PARAMS:
                    raise _malformed()
                first = False
                continue
            if not after_comma:
                raise _malformed()
            self._pos = start
            break
        return Challenge(scheme.lower(), MappingProxyType(params))

    def _token(self) -> str:
        start = self._pos
        while self._pos < len(self._text) and self._text[self._pos] in _TOKEN_CHARS:
            self._pos += 1
        if self._pos == start:
            raise _malformed()
        return self._text[start : self._pos]

    def _value(self) -> str:
        if self._pos < len(self._text) and self._text[self._pos] == '"':
            return self._quoted()
        start = self._pos
        while self._pos < len(self._text) and self._text[self._pos] in _VALUE_CHARS:
            self._pos += 1
        return self._text[start : self._pos]

    def _quoted(self) -> str:
        self._pos += 1
        out: list[str] = []
        while self._pos < len(self._text):
            char = self._text[self._pos]
            self._pos += 1
            if char == '"':
                return "".join(out)
            if char == "\\":
                if self._pos >= len(self._text):
                    break
                char = self._text[self._pos]
                self._pos += 1
            out.append(char)
        raise _malformed()

    def _skip(self, chars: str) -> None:
        while self._pos < len(self._text) and self._text[self._pos] in chars:
            self._pos += 1


def _malformed() -> ChallengeInvalid:
    return ChallengeInvalid("The authentication challenge is malformed.")
