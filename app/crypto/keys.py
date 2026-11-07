"""Master key loading, the versioned keyring, and the key fingerprint."""

import base64
import hashlib
import hmac
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.config import SECRET_KEY_FILE_VAR, SECRET_KEY_VAR, ConfigError, Settings, load_settings
from app.crypto.envelope import KEY_BYTES

FINGERPRINT_CONSTANT = b"dockmin/key-fingerprint/1"
MANIFEST_MAC_CONSTANT = b"dockmin/archive-manifest/1"
FINGERPRINT_HEX_CHARS = 16


@dataclass(frozen=True, slots=True, repr=False)
class MasterKeys:
    """The configured key and, during a re-key window, the one it replaces."""

    current: bytes = field(repr=False)
    previous: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for key in (self.current, self.previous):
            if key is not None and len(key) != KEY_BYTES:
                raise ValueError(f"keys must be exactly {KEY_BYTES} bytes")

    def __repr__(self) -> str:
        return "MasterKeys(<redacted>)"

    @classmethod
    def from_settings(cls, settings: Settings) -> "MasterKeys":
        """Take the keys from settings that name an explicit key source.

        Raises ConfigError when no key is configured.
        """
        if settings.encryption_key is None:
            raise ConfigError(
                f"no key is configured: set {SECRET_KEY_VAR} or {SECRET_KEY_FILE_VAR}",
                (SECRET_KEY_VAR, SECRET_KEY_FILE_VAR),
            )
        return cls(settings.encryption_key, settings.previous_encryption_key)


def load_master_keys(environ: Mapping[str, str] | None = None) -> MasterKeys:
    """Load keys from environ, or from the process environment when None.

    Raises ConfigError, naming the variable and never its value, when no valid key is set.
    """
    return MasterKeys.from_settings(load_settings(environ))


def key_fingerprint(key: bytes) -> str:
    """Return a short identifier for key that reveals no part of it."""
    digest = hmac.new(key, FINGERPRINT_CONSTANT, hashlib.sha256).hexdigest()
    return digest[:FINGERPRINT_HEX_CHARS]


def key_forms(key: bytes) -> tuple[str, ...]:
    """Return the text encodings a key could be logged in, for log redaction."""
    padded = (base64.b64encode(key).decode("ascii"), base64.urlsafe_b64encode(key).decode("ascii"))
    forms = {*padded, *(form.rstrip("=") for form in padded), key.hex(), key.hex().upper()}
    return tuple(sorted(forms))


def manifest_mac(key: bytes, payload: bytes) -> str:
    """Return the hex MAC of an archive manifest under a key derived from key."""
    mac_key = hmac.new(key, MANIFEST_MAC_CONSTANT, hashlib.sha256).digest()
    return hmac.new(mac_key, payload, hashlib.sha256).hexdigest()


class KeyScanner:
    """Detects any spelling of a set of keys in a byte stream fed to it in chunks."""

    __slots__ = ("_found", "_overlap", "_patterns", "_tail")

    def __init__(self, keys: Iterable[bytes]) -> None:
        patterns: set[bytes] = set()
        for key in keys:
            if not key:
                raise ValueError("a key to scan for must not be empty")
            patterns.add(key)
            patterns.update(form.encode("ascii") for form in key_forms(key))
        self._patterns = tuple(patterns)
        self._overlap = max((len(p) for p in patterns), default=1) - 1
        self._tail = b""
        self._found = False

    def __repr__(self) -> str:
        return "KeyScanner(<redacted>)"

    @property
    def found(self) -> bool:
        """Report whether any key has appeared in the stream so far."""
        return self._found

    def feed(self, chunk: bytes) -> None:
        """Scan the next chunk, including a key that straddles it and the previous one."""
        if self._found:
            return
        window = self._tail + chunk
        if any(pattern in window for pattern in self._patterns):
            self._found = True
            self._tail = b""
            return
        self._tail = window[-self._overlap :] if self._overlap else b""


@dataclass(frozen=True, slots=True, repr=False)
class Keyring:
    """The keys readable by version, and the version new values are written at."""

    current_version: int
    keys: Mapping[int, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        if self.current_version not in self.keys:
            raise ValueError("the keyring must hold the current version's key")

    def __repr__(self) -> str:
        return f"Keyring(current_version={self.current_version}, <redacted>)"

    @property
    def current_key(self) -> bytes:
        """Return the key new values are encrypted under."""
        return self.keys[self.current_version]
