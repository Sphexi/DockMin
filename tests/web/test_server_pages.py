"""The certificate page and the service behind it."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from httpx import AsyncClient

from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.config import SELF_SIGNED_DIRECTORY, Settings
from app.services.server_tls import UNREADABLE_FILE, certificate_view
from app.tls import read_certificate
from app.tls_self_signed import SELF_SIGNED_VALIDITY, ensure_self_signed
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, STANDARD_ID, STANDARD_USERNAME
from tests.docker_engine.tls_material import key_of, key_pem
from tests.tls_support import ServerFiles, write_server_files

PAGE = "/server/certificate"
DAY = timedelta(days=1)
ADMIN = Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")
STANDARD = Principal(STANDARD_ID, STANDARD_USERNAME, "standard")
TLS_NOT_CONFIGURED = "TLS is not configured"
NOT_SERVING = "This process does not serve HTTPS."
RESTART_NOTICE = "Restart dockmin to serve the replaced certificate."
UNPARSEABLE_CONTENT = "not a certificate"
SELF_SIGNED_SOURCE = "Generated self-signed certificate"
SUPPLIED_SOURCE = "Supplied file"
BROWSER_WARNING = "Browsers warn about this certificate until it is trusted or replaced."
SELF_SIGNED_RENEWAL = "Restart dockmin to generate a new certificate."
SUPPLIED_RENEWAL = "Replace the certificate file, then restart dockmin."


def window(start: timedelta, end: timedelta) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {"not_before": now + start, "not_after": now + end}


def with_tls(app: FastAPI, files: ServerFiles, *, loaded: bool = True) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"tls_cert_file": files.cert, "tls_key_file": files.key}
    )
    app.state.served_certificate = read_certificate(files.cert) if loaded else None


def with_self_signed(app: FastAPI, *, remaining: timedelta) -> Path:
    """Serve a generated certificate with `remaining` left, and return its file."""
    settings = app.state.settings.model_copy(update={"tls_self_signed": True})
    app.state.settings = settings
    now = datetime.now(UTC) - (SELF_SIGNED_VALIDITY - remaining)
    pair = ensure_self_signed(settings.data_dir / SELF_SIGNED_DIRECTORY, now)
    app.state.served_certificate = pair.tls[1]
    return pair.cert_file


async def test_the_page_says_tls_is_terminated_in_front_when_no_certificate_is_configured(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get(PAGE)

    assert response.status_code == 200
    assert TLS_NOT_CONFIGURED in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "Certificate loaded at start" not in response.text


async def test_only_an_administrator_sees_the_page(
    standard_client: AsyncClient, anonymous_client: AsyncClient
) -> None:
    standard = await standard_client.get(PAGE)
    anonymous = await anonymous_client.get(PAGE)

    assert standard.status_code == 403
    assert (anonymous.status_code, anonymous.headers["location"].split("?")[0]) == (303, "/login")


async def test_the_page_shows_the_loaded_certificate_and_never_any_key_material(
    started_app: FastAPI, admin_client: AsyncClient, tmp_path: Path
) -> None:
    files = write_server_files(tmp_path, **window(-DAY, 200 * DAY))
    with_tls(started_app, files)
    key = key_of("p256")
    private_der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    response = await admin_client.get(PAGE)

    text = response.text
    for fact in (
        "CN=dockmin.example.test",
        "DNS:dockmin.example.test",
        "IP:192.0.2.10",
        "EC secp256r1, 256 bits",
        "ABCDEF",
        "Valid",
    ):
        assert fact in text
    assert read_certificate(files.cert).sha256_fingerprint in text
    assert "PRIVATE KEY" not in text
    assert key_pem(key).splitlines()[1] not in text
    assert private_der.hex() not in text
    assert "Certificate file on disk now" not in text


@pytest.mark.parametrize(
    ("start", "end", "phrase"),
    [
        pytest.param(-DAY, 10 * DAY, "Expires within 30 days", id="expiring"),
        pytest.param(-10 * DAY, -DAY, "The certificate expired on", id="expired"),
        pytest.param(DAY, 100 * DAY, "The certificate is not valid until", id="not-yet-valid"),
    ],
)
async def test_a_certificate_outside_its_good_window_is_flagged_on_the_page(
    started_app: FastAPI,
    admin_client: AsyncClient,
    tmp_path: Path,
    start: timedelta,
    end: timedelta,
    phrase: str,
) -> None:
    with_tls(started_app, write_server_files(tmp_path, **window(start, end)))

    text = (await admin_client.get(PAGE)).text

    assert phrase in text or phrase.replace("Expires", "expires") in text


async def test_a_certificate_replaced_on_disk_is_reported_with_a_restart_notice(
    started_app: FastAPI, admin_client: AsyncClient, tmp_path: Path
) -> None:
    files = write_server_files(tmp_path, **window(-DAY, 200 * DAY))
    with_tls(started_app, files)
    write_server_files(tmp_path, key_variant=1, common_name="replacement.example.test")

    text = (await admin_client.get(PAGE)).text

    assert RESTART_NOTICE in text
    assert "Certificate file on disk now" in text
    assert "CN=replacement.example.test" in text


async def test_a_certificate_file_that_cannot_be_read_now_is_reported_without_echoing_it(
    started_app: FastAPI, admin_client: AsyncClient, tmp_path: Path
) -> None:
    files = write_server_files(tmp_path, **window(-DAY, 200 * DAY))
    with_tls(started_app, files)
    files.cert.write_text(UNPARSEABLE_CONTENT, encoding="ascii")

    text = (await admin_client.get(PAGE)).text

    assert UNREADABLE_FILE in text
    assert UNPARSEABLE_CONTENT not in text
    assert "Certificate loaded at start" in text


async def test_a_process_that_loaded_no_certificate_says_so_and_shows_the_file_on_disk(
    started_app: FastAPI, admin_client: AsyncClient, tmp_path: Path
) -> None:
    with_tls(started_app, write_server_files(tmp_path), loaded=False)

    text = (await admin_client.get(PAGE)).text

    assert NOT_SERVING in text
    assert "Certificate file on disk now" in text
    assert "Certificate loaded at start" not in text


async def test_the_navigation_links_to_the_page_for_an_administrator(
    admin_client: AsyncClient,
) -> None:
    page = await admin_client.get("/users")

    assert f'href="{PAGE}"' in page.text


async def test_names_in_the_certificate_are_shown_as_text_and_not_as_markup(
    started_app: FastAPI, admin_client: AsyncClient, tmp_path: Path
) -> None:
    files = write_server_files(
        tmp_path, common_name="<script>alert(1)</script>", hosts=("a.example.test",)
    )
    with_tls(started_app, files)

    text = (await admin_client.get(PAGE)).text

    assert "alert(1)</script>" not in text
    assert "&lt;script" in text


def settings_with(settings: Settings, files: ServerFiles) -> Settings:
    return settings.model_copy(update={"tls_cert_file": files.cert, "tls_key_file": files.key})


async def test_the_view_is_for_administrators_only(settings: Settings, tmp_path: Path) -> None:
    files = write_server_files(tmp_path)

    with pytest.raises(PermissionDenied):
        await certificate_view(STANDARD, settings_with(settings, files), None, datetime.now(UTC))


async def test_the_view_is_empty_without_tls(settings: Settings) -> None:
    assert await certificate_view(ADMIN, settings, None, datetime.now(UTC)) is None


async def test_the_view_reports_the_expiry_state_and_days_left_of_the_loaded_certificate(
    settings: Settings, tmp_path: Path
) -> None:
    files = write_server_files(tmp_path, **window(-DAY, 10 * DAY + timedelta(hours=1)))
    served = read_certificate(files.cert)

    view = await certificate_view(ADMIN, settings_with(settings, files), served, datetime.now(UTC))

    assert view is not None
    assert (view.state, view.days_left, view.replaced) == ("expiring", 10, False)
    assert view.on_disk is None and view.disk_problem is None


async def test_the_view_shows_the_file_only_when_it_differs_from_what_was_loaded(
    settings: Settings, tmp_path: Path
) -> None:
    files = write_server_files(tmp_path)
    served = read_certificate(files.cert)
    same = await certificate_view(ADMIN, settings_with(settings, files), served, datetime.now(UTC))
    write_server_files(tmp_path, key_variant=1, common_name="other.example.test")
    changed = await certificate_view(
        ADMIN, settings_with(settings, files), served, datetime.now(UTC)
    )

    assert same is not None and same.on_disk is None and not same.replaced
    assert changed is not None and changed.replaced
    assert changed.on_disk is not None and changed.on_disk.subject == "CN=other.example.test"


async def test_the_view_reports_a_file_that_is_gone_as_a_problem_with_no_path_in_it(
    settings: Settings, tmp_path: Path
) -> None:
    files = write_server_files(tmp_path)
    served = read_certificate(files.cert)
    files.cert.unlink()

    view = await certificate_view(ADMIN, settings_with(settings, files), served, datetime.now(UTC))

    assert view is not None
    assert view.disk_problem == UNREADABLE_FILE
    assert str(files.cert) not in (view.disk_problem or "")
    assert not view.replaced


async def test_the_view_never_opens_the_key_file(settings: Settings, tmp_path: Path) -> None:
    files = write_server_files(tmp_path)
    served = read_certificate(files.cert)
    files.key.unlink()

    view = await certificate_view(ADMIN, settings_with(settings, files), served, datetime.now(UTC))

    assert view is not None and view.disk_problem is None


async def test_a_generated_certificate_is_shown_with_its_file_and_a_browser_warning(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    cert_file = with_self_signed(started_app, remaining=200 * DAY)

    text = (await admin_client.get(PAGE)).text

    assert SELF_SIGNED_SOURCE in text and SUPPLIED_SOURCE not in text
    assert f'<dd class="mono">{cert_file}</dd>' in text
    assert cert_file == started_app.state.settings.served_cert_file
    assert BROWSER_WARNING in text and "tls_certificate_self_signed" in text
    assert read_certificate(cert_file).sha256_fingerprint in text
    assert "DNS:localhost" in text and "IP:127.0.0.1" in text
    assert SELF_SIGNED_RENEWAL not in text.split("Certificate loaded at start")[0]


async def test_an_expiring_generated_certificate_says_a_restart_renews_it(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    with_self_signed(started_app, remaining=10 * DAY)

    text = (await admin_client.get(PAGE)).text

    assert "Expires within 30 days" in text
    assert SELF_SIGNED_RENEWAL in text.split("Certificate loaded at start")[0]
    assert SUPPLIED_RENEWAL not in text


async def test_a_supplied_certificate_is_shown_with_its_file_and_a_replace_and_restart_notice(
    started_app: FastAPI, admin_client: AsyncClient, tmp_path: Path
) -> None:
    files = write_server_files(tmp_path, **window(-DAY, 10 * DAY))
    with_tls(started_app, files)

    text = (await admin_client.get(PAGE)).text

    assert SUPPLIED_SOURCE in text and SELF_SIGNED_SOURCE not in text
    assert f'<dd class="mono">{files.cert}</dd>' in text
    assert BROWSER_WARNING not in text
    assert SUPPLIED_RENEWAL in text and SELF_SIGNED_RENEWAL not in text


async def test_the_view_of_a_generated_certificate_reads_the_generated_file(
    settings: Settings, tmp_path: Path
) -> None:
    generated = settings.model_copy(update={"tls_self_signed": True, "data_dir": tmp_path})
    pair = ensure_self_signed(tmp_path / SELF_SIGNED_DIRECTORY, datetime.now(UTC))

    view = await certificate_view(ADMIN, generated, pair.tls[1], datetime.now(UTC))

    assert view is not None
    assert (view.state, view.replaced, view.disk_problem) == ("valid", False, None)
    assert view.served == read_certificate(pair.cert_file)
