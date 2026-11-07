"""Part boundaries and digests of an archive prepared for a multipart upload."""

from dataclasses import dataclass
from typing import Final

MULTIPART_PART_BYTES: Final = 64 * 1024**2


@dataclass(frozen=True, slots=True)
class PartDigest:
    """One part of a file: where it starts, how long it is, and its digests."""

    offset: int
    size: int
    sha256_hex: str
    md5_b64: str
