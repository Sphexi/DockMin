"""Generic S3-compatible object storage client."""

from app.object_storage.checks import (
    PERMISSION_NOTE,
    StorageCheck,
    StorageStage,
    check_target,
    stage_message,
)
from app.object_storage.client import (
    MAX_PUT_FILE_BYTES,
    ObjectBody,
    ObjectStorageClient,
    StorageCredentials,
    StorageEndpoint,
)
from app.object_storage.errors import MAX_ERROR_BODY, StorageFailure, StorageFailureKind
from app.object_storage.keys import validate_key, validate_prefix
from app.object_storage.listing import MAX_LIST_BODY, ListedObject, ObjectListing

__all__ = [
    "MAX_ERROR_BODY",
    "MAX_LIST_BODY",
    "MAX_PUT_FILE_BYTES",
    "PERMISSION_NOTE",
    "ListedObject",
    "ObjectBody",
    "ObjectListing",
    "ObjectStorageClient",
    "StorageCheck",
    "StorageCredentials",
    "StorageEndpoint",
    "StorageFailure",
    "StorageFailureKind",
    "StorageStage",
    "check_target",
    "stage_message",
    "validate_key",
    "validate_prefix",
]
