"""The object storage pages: access, forms, the secret, the connection test and deletion."""

import re
from html import unescape
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from httpx import AsyncClient, Response

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Session, get_session
from app.services import assembly
from app.services.activity import ActivityRecorder
from app.services.object_storage_admin import ObjectStorageTargetService
from tests.auth_support import csrf_form
from tests.job_support import execute, rows
from tests.object_storage.support import ACCESS, BUCKET, ENDPOINT, SECRET
from tests.subsystems.syncer_support import Bucket, always_on, error
from tests.web.flash_support import flash_texts, land, location
from tests.web.helpers import field_error, form_values, parse_tags


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | {"DOCKMIN_PERMITTED_INTERNAL_NETWORKS": "127.0.0.0/8,::1/128"}


OK = 200
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
BAD_REQUEST = 400
UNPROCESSABLE = 422
HTMX = {"HX-Request": "true"}
HTMX_TOKEN = {**HTMX, "X-CSRF-Token": "C" * 43}
CREATED_AT = "/object-storage/1"
UNREADABLE_BUNDLE = "-----BEGIN CERTIFICATE-----\nQUFBQUJCQkI=\n-----END CERTIFICATE-----"
CREATED_NOTICE = "The target is saved. Test the connection to check it."
DELETED_NOTICE = "The target is deleted. Objects in its bucket are untouched."
UPDATED_NOTICE = "Changes saved."

STANDARD_GETS = [
    "/object-storage",
    "/object-storage/new",
    "/object-storage/1",
    "/object-storage/1/edit",
    "/object-storage/1/delete",
    "/backups",
]
POSTS = [
    "/object-storage",
    "/object-storage/1",
    "/object-storage/1/test",
    "/object-storage/1/delete",
    "/backups/download",
]
FORM_FIELDS = {
    "name",
    "endpoint_url",
    "region",
    "bucket",
    "access_key_id",
    "secret_access_key",
    "use_path_style",
    "verify_tls",
    "ca_bundle",
    "ca_bundle_file",
    "backup_prefix",
    "activity_log_prefix",
    "retention_count",
    "confirm_retention",
    "csrf_token",
}


def text_of(response: Response | str) -> str:
    html = response if isinstance(response, str) else response.text
    return unescape(re.sub(r"<[^>]+>", " ", html))


def target_form(client: AsyncClient, **changes: str | None) -> dict[str, str]:
    base: dict[str, str | None] = {
        "name": "primary",
        "endpoint_url": ENDPOINT,
        "region": "us-east-1",
        "bucket": BUCKET,
        "access_key_id": ACCESS,
        "secret_access_key": SECRET,
        "use_path_style": "on",
        "verify_tls": "on",
        "backup_prefix": "backups",
        "activity_log_prefix": "activity-log",
        "retention_count": "",
    }
    merged = {name: value for name, value in (base | changes).items() if value is not None}
    return csrf_form(client, **merged)


@pytest.fixture
def bucket(started_app: FastAPI) -> Bucket:
    """Make the target service talk to a fake bucket instead of the network."""
    fake = Bucket()

    async def provide(
        request: Request,
        db: Annotated[Session, Depends(get_session)],
        runtime: Annotated[AuthRuntime, Depends(get_runtime)],
        activity: Annotated[ActivityRecorder, Depends(get_activity)],
    ) -> ObjectStorageTargetService:
        return ObjectStorageTargetService(
            db,
            request.app.state.vault,
            runtime.clock,
            activity,
            transport=httpx.MockTransport(fake.handler),
        )

    started_app.dependency_overrides[assembly.get_object_storage_service] = provide
    return fake


async def create(client: AsyncClient, **changes: str | None) -> Response:
    response = await client.post("/object-storage", data=target_form(client, **changes))
    assert response.status_code == SEE_OTHER, text_of(response)[:300]
    return response


async def stamp_of_edit_form(client: AsyncClient) -> str:
    return form_values((await client.get("/object-storage/1/edit")).text)["expected_updated_at"]


async def save_edit(client: AsyncClient, **changes: str | None) -> Response:
    stamp = await stamp_of_edit_form(client)
    base: dict[str, str | None] = {
        "name": "primary",
        "secret_access_key": "",
        "expected_updated_at": stamp,
    }
    return await client.post("/object-storage/1", data=target_form(client, **(base | changes)))


async def test_a_standard_user_is_refused_every_page_and_action(
    standard_client: AsyncClient,
) -> None:
    gets = {path: (await standard_client.get(path)).status_code for path in STANDARD_GETS}
    posts = {
        path: (await standard_client.post(path, data=csrf_form(standard_client))).status_code
        for path in POSTS
    }

    assert set(gets.values()) == {FORBIDDEN} and set(posts.values()) == {FORBIDDEN}


async def test_an_administrator_without_a_token_and_an_anonymous_visitor_are_turned_away(
    admin_client: AsyncClient, anonymous_client: AsyncClient
) -> None:
    posts = {path: (await admin_client.post(path, data={})).status_code for path in POSTS}
    visitor = await anonymous_client.get("/object-storage")

    assert set(posts.values()) == {FORBIDDEN}
    assert visitor.status_code in (302, 303, 307) and "/login" in visitor.headers["location"]


async def test_the_empty_list_offers_to_add_a_target_and_navigation_follows_the_role(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    listing = await admin_client.get("/object-storage")
    home = await standard_client.get("/")

    assert listing.status_code == OK and "No object storage targets" in listing.text
    assert 'href="/object-storage/new"' in listing.text
    assert listing.headers["cache-control"] == "no-store"
    assert 'href="/object-storage"' in listing.text and 'href="/backups"' in listing.text
    assert 'href="/object-storage"' not in home.text and 'href="/backups"' not in home.text


async def test_the_new_form_has_every_field_its_defaults_and_a_secret_field_that_stays_empty(
    admin_client: AsyncClient,
) -> None:
    html = (await admin_client.get("/object-storage/new")).text

    tags = parse_tags(html)
    names = {t.attrs.get("name") for t in tags if t.name in ("input", "select", "textarea")}
    values = form_values(html)
    secret = next(t for t in tags if t.attrs.get("name") == "secret_access_key")
    assert names >= FORM_FIELDS
    assert (values["region"], values["backup_prefix"], values["activity_log_prefix"]) == (
        "us-east-1",
        "backups",
        "activity-log",
    )
    assert (values["use_path_style"], values["verify_tls"]) == ("on", "on")
    assert secret.attrs["type"] == "password" and "value" not in secret.attrs
    assert secret.attrs["autocomplete"] == "new-password"
    assert 'enctype="multipart/form-data"' in html and 'action="/object-storage"' in html
    assert "server administrator" in html
    assert "<script>" not in html and "style=" not in html


REFUSALS: dict[str, tuple[dict[str, Any], str]] = {
    "retention-unconfirmed": ({"retention_count": "3"}, "Confirm that older backup archives"),
    "bad-endpoint": ({"endpoint_url": "ftp://x"}, "http or https"),
    "missing-secret": ({"secret_access_key": ""}, "Enter the secret access key"),
    "equal-prefixes": ({"backup_prefix": "same", "activity_log_prefix": "same"}, "must differ"),
    "virtual-host-ip": ({"use_path_style": None}, "host name"),
}


async def test_a_form_that_breaks_a_rule_is_refused_with_the_message_beside_its_field(
    admin_client: AsyncClient,
) -> None:
    refused = {
        label: await admin_client.post("/object-storage", data=target_form(admin_client, **changes))
        for label, (changes, _) in REFUSALS.items()
    }

    assert {label: response.status_code for label, response in refused.items()} == dict.fromkeys(
        REFUSALS, UNPROCESSABLE
    )
    assert {
        label: REFUSALS[label][1] in text_of(r) for label, r in refused.items()
    } == dict.fromkeys(REFUSALS, True)
    assert not any(SECRET in response.text for response in refused.values())


async def test_a_refused_form_with_plain_http_unverified_tls_and_a_loopback_host_shows_each_warning(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.post(
        "/object-storage",
        data=target_form(admin_client, verify_tls=None, retention_count="abc"),
    )

    assert response.status_code == UNPROCESSABLE
    assert "unencrypted" in response.text
    assert "not verified" in response.text
    assert "link-local" in response.text


async def test_an_unreadable_ca_bundle_is_reported_together_with_the_other_field_errors(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.post(
        "/object-storage", data=target_form(admin_client, name="", ca_bundle=UNREADABLE_BUNDLE)
    )

    assert response.status_code == UNPROCESSABLE
    assert field_error(response.text, "field-name") is not None
    assert "could not be read" in (field_error(response.text, "field-ca_bundle") or "")


async def test_creating_a_target_opens_its_page_with_the_facts_and_never_the_secret(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    created = await create(admin_client, retention_count="3", confirm_retention="on")

    detail = await land(admin_client, created)
    text = text_of(detail)

    assert location(created) == CREATED_AT
    assert detail.status_code == OK
    assert "primary" in text and ACCESS in text
    assert flash_texts(detail.text) == [CREATED_NOTICE]
    assert "Stored. It is never shown." in text
    assert SECRET not in detail.text
    assert "unencrypted" in text and "link-local" in text
    assert "The newest 3 of each rule" in text and "dockmin-backup-" in text
    assert 'action="/object-storage/1/test"' in detail.text
    assert 'hx-post="/object-storage/1/test"' in detail.text
    assert "does not check write or delete permission" in text


async def test_the_list_shows_a_created_target_as_a_page_and_as_a_table_fragment(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client, retention_count="3", confirm_retention="on")

    page = await admin_client.get("/object-storage")
    fragment = await admin_client.get("/object-storage", headers=HTMX)
    bad_cursor = await admin_client.get("/object-storage?cursor=zzz")

    assert "primary" in page.text and "Plain http" in page.text
    assert 'href="/object-storage/1"' in page.text
    assert "<html" not in fragment.text and 'id="targets-table"' in fragment.text
    assert bad_cursor.status_code == BAD_REQUEST


async def test_a_second_target_with_the_same_name_in_any_case_is_a_field_error(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client)

    response = await admin_client.post(
        "/object-storage", data=target_form(admin_client, name="PRIMARY")
    )

    assert response.status_code == UNPROCESSABLE and "already exists" in text_of(response)


async def test_creating_a_target_is_audited_without_the_secret_or_the_access_key(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    await create(admin_client)

    events = await rows(
        started_app,
        "SELECT action, detail FROM activity_event WHERE action LIKE 'object_storage_target%'",
    )
    assert [e["action"] for e in events] == ["object_storage_target_created"]
    assert SECRET not in events[0]["detail"] and ACCESS not in events[0]["detail"]


async def test_the_edit_form_is_filled_with_the_stored_values_and_carries_the_reviewed_stamp(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client, retention_count="3", confirm_retention="on")

    response = await admin_client.get("/object-storage/1/edit")

    values = form_values(response.text)
    assert (values["name"], values["bucket"], values["retention_count"]) == ("primary", BUCKET, "3")
    assert values["expected_updated_at"]
    assert SECRET not in response.text and "leave blank to keep it" in response.text
    assert "unencrypted" in response.text


async def test_renaming_with_a_blank_secret_saves_and_changing_the_bucket_without_it_is_refused(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client)

    renamed = await save_edit(admin_client, name="renamed")
    landing = await land(admin_client, renamed)
    moved = await save_edit(admin_client, name="renamed", bucket="other")

    assert (renamed.status_code, location(renamed)) == (SEE_OTHER, CREATED_AT)
    assert flash_texts(landing.text) == [UPDATED_NOTICE]
    assert moved.status_code == UNPROCESSABLE
    assert "enter the secret access key again" in text_of(moved)
    assert "never sent back to the browser" in moved.text and SECRET not in moved.text


async def test_lowering_retention_needs_the_confirmation_and_raising_it_does_not(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client, retention_count="3", confirm_retention="on")

    unconfirmed = await save_edit(admin_client, retention_count="2")
    confirmed = await save_edit(admin_client, retention_count="2", confirm_retention="on")
    raised = await save_edit(admin_client, retention_count="9")

    assert unconfirmed.status_code == UNPROCESSABLE
    assert "Confirm that older backup archives" in text_of(unconfirmed)
    assert (confirmed.status_code, raised.status_code) == (SEE_OTHER, SEE_OTHER)


async def test_a_stale_edit_form_is_refused_and_the_page_reloads_the_stored_values(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client)
    stale = await stamp_of_edit_form(admin_client)
    await save_edit(admin_client, name="renamed")

    response = await admin_client.post(
        "/object-storage/1",
        data=target_form(
            admin_client, name="stale", secret_access_key="", expected_updated_at=stale
        ),
    )

    values = form_values(response.text)
    assert response.status_code == UNPROCESSABLE and "Changed since it was reviewed" in text_of(
        response
    )
    assert values["name"] == "renamed" and values["expected_updated_at"] != stale


async def test_a_changed_bucket_with_the_secret_entered_again_is_saved(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client)

    response = await save_edit(admin_client, bucket="other", secret_access_key=SECRET)

    assert response.status_code == SEE_OTHER


async def test_an_unknown_or_malformed_target_id_is_not_found(
    admin_client: AsyncClient,
) -> None:
    unknown = await admin_client.get("/object-storage/999")
    malformed = await admin_client.get("/object-storage/abc")

    assert unknown.status_code == NOT_FOUND
    assert malformed.status_code in (NOT_FOUND, UNPROCESSABLE)


async def test_a_certificate_bundle_that_is_not_pem_is_a_field_error(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client)

    response = await save_edit(admin_client, ca_bundle="not a pem")

    assert response.status_code == UNPROCESSABLE


async def test_the_connection_test_reports_each_stage_in_the_products_words(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client)

    page = await admin_client.post("/object-storage/1/test", data=csrf_form(admin_client))
    fragment = await admin_client.post("/object-storage/1/test", headers=HTMX_TOKEN, data={})

    text = text_of(page)
    assert page.status_code == OK and "Every stage passed." in text
    assert "access key, the secret and the bucket name are accepted" in text
    assert "can list the backup prefix" in text and "can list the activity log prefix" in text
    assert "does not check write or delete permission" in text
    assert "<html" not in fragment.text and 'id="object-storage-test"' in fragment.text


async def test_a_refused_backup_listing_fails_that_stage_with_the_retention_explanation(
    admin_client: AsyncClient, bucket: Bucket
) -> None:
    await create(admin_client)
    bucket.script = [
        lambda r: (
            error(403, "AccessDenied")(r)
            if r.method == "GET" and "backups" in r.url.query.decode()
            else None
        )
    ]

    response = await admin_client.post("/object-storage/1/test", data=csrf_form(admin_client))

    text = text_of(response)
    assert "1 of 3 stage" in text and "remote retention cannot run" in text
    assert "can list the activity log prefix" in text and "HTTP 403, AccessDenied" in text


async def test_a_bucket_that_refuses_everything_fails_all_three_stages_and_each_still_ran(
    admin_client: AsyncClient, bucket: Bucket, started_app: FastAPI
) -> None:
    await create(admin_client)
    bucket.script = [
        always_on("HEAD", error(403, "AccessDenied")),
        always_on("GET", error(403, "AccessDenied")),
    ]

    response = await admin_client.post("/object-storage/1/test", data=csrf_form(admin_client))

    tested = await rows(
        started_app,
        "SELECT result FROM activity_event WHERE action = 'object_storage_target_tested'",
    )
    text = text_of(response)
    assert "3 of 3 stages failed" in text
    assert "cannot tell a missing bucket from a credential without access" in text
    assert [t["result"] for t in tested] == ["success"]


async def reference(app: FastAPI, name: str, subsystem: str) -> None:
    await execute(
        app,
        "INSERT INTO automation_rule (name, subsystem, target_selector, parameters, created_at, "
        "updated_at) VALUES (?, ?, '', '{\"object_storage_target_id\":1}', 'x', 'x')",
        (name, subsystem),
    )


async def test_a_target_a_rule_uses_cannot_be_deleted_and_the_pages_list_the_rules(
    admin_client: AsyncClient, started_app: FastAPI, bucket: Bucket
) -> None:
    await create(admin_client)
    await reference(started_app, "Nightly backup", "backup-syncer")
    await reference(started_app, "Export", "activity-log-exporter")

    confirm = await admin_client.get("/object-storage/1/delete")
    refused = await admin_client.post(
        "/object-storage/1/delete", data=csrf_form(admin_client, expected_name="primary")
    )
    detail = await admin_client.get("/object-storage/1")

    assert "Nightly backup" in confirm.text and "Export" in confirm.text
    assert 'name="expected_name"' not in confirm.text and "cannot be deleted" in confirm.text
    assert refused.status_code == UNPROCESSABLE
    assert "Automation rules use this target" in text_of(refused)
    assert "Nightly backup" in detail.text and "backup-syncer" in detail.text
    assert len(await rows(started_app, "SELECT 1 FROM object_storage_target")) == 1


async def test_deleting_an_unused_target_needs_its_reviewed_name_and_reports_the_deletion(
    admin_client: AsyncClient, started_app: FastAPI, bucket: Bucket
) -> None:
    await create(admin_client)

    confirm = await admin_client.get("/object-storage/1/delete")
    wrong = await admin_client.post(
        "/object-storage/1/delete", data=csrf_form(admin_client, expected_name="wrong")
    )
    done = await admin_client.post(
        "/object-storage/1/delete", data=csrf_form(admin_client, expected_name="primary")
    )
    listing = await land(admin_client, done)

    assert (
        'name="expected_name"' in confirm.text
        and "Objects in the bucket are not touched" in confirm.text
    )
    assert 'value="primary"' in confirm.text
    assert wrong.status_code == UNPROCESSABLE
    assert (done.status_code, location(done)) == (SEE_OTHER, "/object-storage")
    assert flash_texts(listing.text) == [DELETED_NOTICE]
    assert await rows(started_app, "SELECT 1 FROM object_storage_target") == []
