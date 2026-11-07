"""Companion files as shown, the candidate sets a write is checked with, and the in-use check."""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from app.db.repositories.stack import StackKind, StoredStack
from app.db.repositories.stack_file import (
    StackFileError,
    StackFileLimitError,
    StackFileRepository,
    StoredFile,
)
from app.services.stack_deploy import StackRejected
from app.services.stack_document import (
    CompanionFile,
    ComposeDocumentRejected,
    FileUse,
    FileUses,
    decode_file,
    file_name,
    precheck,
)
from app.services.stack_views import FileView

__all__ = [
    "KEEP_SECRET_MESSAGE",
    "candidate_with",
    "candidate_without",
    "checked_content",
    "checked_file_name",
    "checked_uses",
    "file_in_use",
    "file_kind",
    "file_view",
    "file_views",
    "secret_file_names",
    "stored_file_error",
    "stored_placeholders",
    "uses_of",
]

KEEP_SECRET_MESSAGE = "Enter the file's content."

_PLACEHOLDER = "x"


def stored_placeholders(items: Sequence[StoredFile]) -> tuple[CompanionFile, ...]:
    """Return the stored files with a secret's content replaced, so nothing is decrypted.

    The result answers where each file is named, which does not depend on a secret's content.
    """
    return tuple(
        CompanionFile(
            item.name,
            _PLACEHOLDER if item.is_secret else item.content or _PLACEHOLDER,
            item.is_secret,
        )
        for item in items
    )


def _any_build(base: str, service: Mapping[Any, Any]) -> Mapping[str, str]:
    return {}


async def uses_of(text: str, files: Sequence[CompanionFile], kind: StackKind) -> FileUses:
    """Return where the documents name each file; empty when the text fails the pre-check.

    A `build` section is not judged here: this only finds where files are named.
    """
    try:
        return await asyncio.to_thread(precheck, text, kind=kind, files=files, builds=_any_build)
    except ComposeDocumentRejected:
        return {}


async def checked_uses(text: str, files: Sequence[CompanionFile], kind: StackKind) -> FileUses:
    """Return where the documents name each file; StackRejected when the pre-check refuses."""
    try:
        return await asyncio.to_thread(precheck, text, kind=kind, files=files)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason, lines=rejected.lines) from None


def file_view(item: StoredFile, uses: Sequence[FileUse]) -> FileView:
    """Describe one stored file; a secret's size, digest and content are withheld."""
    if item.is_secret:
        return FileView(item.name, True, None, None, None, tuple(uses))
    return FileView(
        item.name, False, item.size_bytes, item.content_digest, item.content, tuple(uses)
    )


async def file_views(repository: StackFileRepository, stack: StoredStack) -> tuple[FileView, ...]:
    """Describe a stack's files by name, each with the places its documents name it."""
    items = await repository.files(stack.id)
    if not items:
        return ()
    uses = await uses_of(stack.compose_file, stored_placeholders(items), stack.kind)
    return tuple(file_view(item, uses.get(item.name, ())) for item in items)


def checked_file_name(text: str) -> str:
    """Return the trimmed file name; StackRejected keyed `name` when it breaks the grammar."""
    try:
        return file_name(text)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None


def checked_content(content: str | None) -> str | None:
    """Return the content after the text rules; StackRejected keyed `content` when it fails."""
    if content is None:
        return None
    try:
        return decode_file(content)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None


def candidate_with(
    stored: Sequence[CompanionFile], name: str, content: str | None, secret: bool
) -> tuple[CompanionFile, ...]:
    """Return the stored files with `name` added or replaced.

    A secret given no content keeps the content stored under that name; StackRejected keyed
    `content` for any other file given none.
    """
    kept = next((item for item in stored if item.name == name), None)
    if content is None:
        if kept is None or not (kept.secret and secret):
            raise StackRejected({"content": KEEP_SECRET_MESSAGE}, "invalid")
        return tuple(stored)
    others = tuple(item for item in stored if item.name != name)
    return (*others, CompanionFile(name, content, secret))


def candidate_without(stored: Sequence[CompanionFile], name: str) -> tuple[CompanionFile, ...]:
    """Return the stored files without `name`."""
    return tuple(item for item in stored if item.name != name)


def file_in_use(name: str, uses: Sequence[FileUse]) -> StackRejected:
    """Return the refusal for removing a file that a document still names."""
    where = ", ".join(sorted({use.path for use in uses}))
    return StackRejected({"file": f"{name} is still used at {where}."}, "file_in_use")


def secret_file_names(files: Sequence[CompanionFile]) -> str:
    """Return the names of the secret files, sorted and comma-separated; never their content."""
    return ",".join(sorted(item.name for item in files if item.secret))


def stored_file_error(error: StackFileError) -> StackRejected:
    """Return the refusal for a file write the repository declined."""
    if isinstance(error, StackFileLimitError):
        return StackRejected(
            {"files": "A stack holds at most 20 files and 128 KiB in total."}, "too_large"
        )
    return StackRejected({"files": "A file could not be stored."}, "invalid")


def file_kind(secret: bool) -> str:
    """Return the audit value for a file's kind; a key ending in `_secret` would be redacted."""
    return "secret" if secret else "plain"
