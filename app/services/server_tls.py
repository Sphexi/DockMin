"""The server certificate as the certificate page shows it."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.auth.principal import Principal
from app.config import Settings
from app.services.guards import require_administrator
from app.tls import (
    CertificateSummary,
    CertificateUnusable,
    ExpiryState,
    expiry_state,
    read_certificate,
)

UNREADABLE_FILE: Final = "The certificate file cannot be read now."


@dataclass(frozen=True, slots=True)
class CertificateView:
    """The certificate this process loaded, and the file as it is on disk now."""

    served: CertificateSummary | None
    on_disk: CertificateSummary | None
    disk_problem: str | None
    state: ExpiryState | None
    days_left: int | None
    replaced: bool


async def _read_file_now(settings: Settings) -> tuple[CertificateSummary | None, str | None]:
    """Read the certificate file as it is on disk; the key file is never opened."""
    cert_file = settings.served_cert_file
    assert cert_file is not None
    try:
        return await asyncio.to_thread(read_certificate, cert_file), None
    except CertificateUnusable:
        return None, UNREADABLE_FILE


async def certificate_view(
    principal: Principal,
    settings: Settings,
    served: CertificateSummary | None,
    now: datetime,
) -> CertificateView | None:
    """Return the certificate details, or None when TLS is not configured; administrators only.

    served is the certificate the listener loaded, or None when this process does not serve HTTPS.
    The on-disk file is returned when it differs from the loaded certificate or none is loaded.
    """
    require_administrator(principal)
    if not settings.tls_enabled:
        return None
    on_disk, disk_problem = await _read_file_now(settings)
    replaced = (
        served is not None
        and on_disk is not None
        and on_disk.sha256_fingerprint != served.sha256_fingerprint
    )
    return CertificateView(
        served=served,
        on_disk=on_disk if replaced or served is None else None,
        disk_problem=disk_problem,
        state=expiry_state(served, now) if served is not None else None,
        days_left=(served.not_after - now).days if served is not None else None,
        replaced=replaced,
    )
