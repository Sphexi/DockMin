"""Constants and small helpers shared by test modules."""

import base64

from app.auth.passwords import HashParameters
from app.config import ENV_PREFIX, KEY_BYTES

KEY_MATERIAL = bytes(range(KEY_BYTES))
VALID_KEY = base64.b64encode(KEY_MATERIAL).decode("ascii")

# The smallest cost the settings accept; the default costs tens of milliseconds a hash.
CHEAP_ARGON2 = HashParameters(memory_kib=8_192, iterations=1, lanes=1, salt_bytes=16, hash_bytes=32)
CHEAP_ARGON2_ENV = {
    f"{ENV_PREFIX}ARGON2_MEMORY_KIB": str(CHEAP_ARGON2.memory_kib),
    f"{ENV_PREFIX}ARGON2_ITERATIONS": str(CHEAP_ARGON2.iterations),
    f"{ENV_PREFIX}ARGON2_LANES": str(CHEAP_ARGON2.lanes),
}

MAX_LOGIN_FAILURES = 3
LOCKOUT_ENV = {f"{ENV_PREFIX}LOGIN_MAX_FAILURES": str(MAX_LOGIN_FAILURES)}

TRUSTED_PROXY_ENV = {f"{ENV_PREFIX}TRUSTED_PROXIES": "203.0.113.200"}
INSECURE_COOKIE_ENV = {f"{ENV_PREFIX}COOKIE_SECURE": "false"}


def present[T](value: T | None) -> T:
    """Return a value the test has just arranged to exist, narrowed from its optional type."""
    assert value is not None
    return value
