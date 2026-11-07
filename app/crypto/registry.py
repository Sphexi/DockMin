"""The single declared list of every database column that holds a secret."""

from dataclasses import dataclass

from app.crypto.envelope import PLAIN_IDENTIFIER


@dataclass(frozen=True, slots=True)
class SecretField:
    """A (table, column) pair holding an encrypted value."""

    table: str
    column: str

    def __post_init__(self) -> None:
        for name in (self.table, self.column):
            if PLAIN_IDENTIFIER.fullmatch(name) is None:
                raise ValueError("secret field names must be plain lowercase identifiers")

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"


REGISTRY_CREDENTIAL_SECRET = SecretField("registry_credential", "secret_ciphertext")
REGISTRY_CREDENTIAL_DERIVED_SECRET = SecretField("registry_credential", "derived_secret_ciphertext")
ENVIRONMENT_TLS_CLIENT_KEY = SecretField("environment_tls_material", "client_key_ciphertext")
OBJECT_STORAGE_SECRET_ACCESS_KEY = SecretField(
    "object_storage_target", "secret_access_key_ciphertext"
)
STACK_VARIABLE_VALUE = SecretField("stack_variable", "value_ciphertext")
STACK_FILE_CONTENT = SecretField("stack_file", "content_ciphertext")
NOTIFICATION_CHANNEL_SECRETS = SecretField("notification_channel", "secrets_ciphertext")

SECRET_FIELDS: tuple[SecretField, ...] = (
    REGISTRY_CREDENTIAL_SECRET,
    REGISTRY_CREDENTIAL_DERIVED_SECRET,
    ENVIRONMENT_TLS_CLIENT_KEY,
    OBJECT_STORAGE_SECRET_ACCESS_KEY,
    STACK_VARIABLE_VALUE,
    STACK_FILE_CONTENT,
    NOTIFICATION_CHANNEL_SECRETS,
)
