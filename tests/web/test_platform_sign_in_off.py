"""With the feature off the header is never read and nothing is fetched."""

import socket
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.jwt_identity import JwtVerifier
from app.auth.platform_signin import PlatformIdentity
from app.services.jwks_fetcher import JwksFetcher
from tests.auth_support import STANDARD_ID, Browsers
from tests.jwt_support import EMAIL, HEADER, ec_signer
from tests.web.platform_support import ACCOUNT_PATH, SEE_OTHER, SEEDED_SESSIONS, session_count


def _tripwire(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the access platform code ran while the feature is off")


async def test_nothing_is_read_verified_or_fetched_while_the_feature_is_off(
    started_app: FastAPI, browsers: Browsers, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = ec_signer()
    async with started_app.state.database.session() as db:
        await db.execute("UPDATE user SET external_identity = ? WHERE id = ?", (EMAIL, STANDARD_ID))
    monkeypatch.setattr(JwtVerifier, "verify", _tripwire)
    monkeypatch.setattr(JwksFetcher, "fetch", _tripwire)
    monkeypatch.setattr(PlatformIdentity, "assertion", _tripwire)
    monkeypatch.setattr(socket.socket, "connect", _tripwire)
    monkeypatch.setattr(socket, "getaddrinfo", _tripwire)
    client = browsers.open()

    page = await client.get(ACCOUNT_PATH, headers={HEADER: signer.token()})
    login = await client.get("/login", headers={HEADER: signer.token()})
    public = await client.get("/api/settings/public")

    assert started_app.state.auth.platform is None
    assert (page.status_code, page.headers["location"]) == (SEE_OTHER, "/login?next=%2Faccount")
    assert "Continue as" not in login.text
    assert public.json()["authentication_methods"] == ["internal"]
    assert await session_count(started_app) == SEEDED_SESSIONS
