"""Async Docker Engine API client: transports, negotiation, typed calls, streams, errors."""

from app.docker_engine.client import EngineClient
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
from app.docker_engine.probe import (
    EngineFacts,
    ProbeResult,
    ProbeStatus,
    ProbeTimeouts,
    Stage,
    StageOutcome,
    StageResult,
    probe_connection,
)
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.timeouts import Timeouts
from app.docker_engine.tls_validation import (
    TlsField,
    TlsMetadata,
    TlsValidation,
    validate_tls_material,
)
from app.docker_engine.transport import (
    EndpointError,
    canonical_host,
    canonical_tcp_endpoint,
    canonical_unix_endpoint,
    split_tcp_endpoint,
    validate_socket_path,
)

__all__ = [
    "Conflict",
    "EndpointError",
    "EngineClient",
    "EngineClientRegistry",
    "EngineError",
    "EngineFacts",
    "EnvironmentUnreachable",
    "NotFound",
    "PermissionDenied",
    "ProbeResult",
    "ProbeStatus",
    "ProbeTimeouts",
    "RequestOutcomeUnknown",
    "ResponseTooLarge",
    "Stage",
    "StageOutcome",
    "StageResult",
    "Timeouts",
    "TlsField",
    "TlsMetadata",
    "TlsValidation",
    "VersionUnsupported",
    "canonical_host",
    "canonical_tcp_endpoint",
    "canonical_unix_endpoint",
    "probe_connection",
    "split_tcp_endpoint",
    "validate_socket_path",
    "validate_tls_material",
]
