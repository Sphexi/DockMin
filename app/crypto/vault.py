"""The object repositories use to seal and open secret values."""

from app.crypto import envelope
from app.crypto.envelope import SecretLocation
from app.crypto.keys import Keyring, KeyScanner, key_fingerprint, manifest_mac
from app.crypto.registry import SecretField


class Vault:
    """Encrypts at the current key version and decrypts at any version the keyring holds."""

    def __init__(self, keyring: Keyring, instance_id: str) -> None:
        if not envelope.is_valid_instance_id(instance_id):
            raise ValueError("instance_id must be 32 lowercase hexadecimal characters")
        self._keyring = keyring
        self._instance_id = instance_id

    def __repr__(self) -> str:
        return f"Vault(current_version={self._keyring.current_version})"

    @property
    def current_version(self) -> int:
        """Return the key version new values are written at."""
        return self._keyring.current_version

    def seal(self, plaintext: str, location: SecretLocation) -> str:
        """Encrypt plaintext for the given row at the current key version."""
        return envelope.encrypt(
            plaintext,
            self._keyring.current_key,
            self._keyring.current_version,
            location,
            self._instance_id,
        )

    def open(self, token: str, location: SecretLocation) -> str:
        """Decrypt a stored value."""
        return envelope.decrypt(token, self._keyring.keys, location, self._instance_id)

    def version_of(self, token: str) -> int:
        """Return the key version a stored value was written at."""
        return envelope.key_version_of(token)

    def reseal(self, token: str, field: SecretField, row_id: int) -> str:
        """Re-encrypt a stored value at the current key version without exposing the plaintext."""
        location = SecretLocation(field.table, field.column, row_id)
        return self.seal(self.open(token, location), location)

    def key_fingerprint(self) -> str:
        """Return the fingerprint of the current key."""
        return key_fingerprint(self._keyring.current_key)

    def manifest_mac(self, payload: bytes) -> str:
        """Return the archive manifest MAC of payload under the current key."""
        return manifest_mac(self._keyring.current_key, payload)

    def key_scanner(self) -> KeyScanner:
        """Return a scanner for every spelling of every key this vault holds."""
        return KeyScanner(self._keyring.keys.values())

    @staticmethod
    def for_restore(
        *, running_key: bytes, archive_key: bytes, archive_version: int, instance_id: str
    ) -> "Vault":
        """Return a vault for a restored database that reads archive_key and writes running_key.

        The running key is written one version above archive_version.
        """
        keys = {archive_version: archive_key, archive_version + 1: running_key}
        return Vault(Keyring(archive_version + 1, keys), instance_id)
