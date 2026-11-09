"""Build contexts held in memory before a stack exists, read the way stored ones are."""

from collections.abc import Mapping
from dataclasses import dataclass

from app.db.repositories.stack_build_context import ContextWrite
from app.services.build_context_archive import read_context_file, run_limited
from app.services.dockerfile_check import MAX_DOCKERFILE_BYTES

__all__ = ["HeldContexts"]


@dataclass(frozen=True, slots=True)
class HeldContexts:
    """Contexts about to be stored with a stack, by name; they satisfy `ContextSource`."""

    items: Mapping[str, ContextWrite]

    async def digests(self) -> dict[str, str]:
        """Return each context's content digest by name."""
        return {name: write.content_digest for name, write in self.items.items()}

    async def read_file(self, name: str, path: str) -> bytes | None:
        """Return a file of a held context, or None when the context or the file is absent."""
        write = self.items.get(name)
        if write is None:
            return None
        return await run_limited(
            read_context_file,
            write.content,
            path,
            digest=write.content_digest,
            max_bytes=MAX_DOCKERFILE_BYTES,
        )
