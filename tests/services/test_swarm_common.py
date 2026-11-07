"""Image text classification and pinning shared by the Swarm services and stacks."""

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.docker_engine.errors import EngineError
from app.services.acting import AutomationAuthority
from app.services.activity import SubsystemActor
from app.services.pull_credentials import PullCredentials
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.swarm_common import pin_image, pinned_text, split_service_image
from tests.access_support import ADMIN

HEX = "a" * 64
OTHER_HEX = "b" * 64
DIGEST = f"sha256:{HEX}"
OTHER_DIGEST = f"sha256:{OTHER_HEX}"
REGISTRY_IMAGE = "registry.example.com/app:1"
SET_ID = 7
ENVIRONMENT_ID = 1
NO_ROUTE_STATUS = 500
UNRESOLVED_AUTH = SecretStr("c2VjcmV0")


@pytest.mark.parametrize(
    ("text", "kind", "reference", "digest"),
    [
        (f"{REGISTRY_IMAGE}@{DIGEST}", "tagged_digest", REGISTRY_IMAGE, DIGEST),
        ("registry.example.com/app@" + DIGEST, "digest_only", "registry.example.com/app", DIGEST),
        (f"busybox:1@{DIGEST}", "tagged_digest", "busybox:1", DIGEST),
        (REGISTRY_IMAGE, "unpinned", REGISTRY_IMAGE, None),
        ("busybox", "unpinned", "busybox", None),
        (f"  {REGISTRY_IMAGE}  ", "unpinned", REGISTRY_IMAGE, None),
        (DIGEST, "image_id", None, None),
        ("0123456789ab", "image_id", None, None),
        (HEX, "image_id", None, None),
        (f"{REGISTRY_IMAGE}@sha256:zz", "unpinned", None, None),
        ("R.io/APP", "unpinned", None, None),
        ("", "unpinned", None, None),
    ],
    ids=[
        "tag-and-digest",
        "digest-only",
        "hub-tag-and-digest",
        "tag-only",
        "bare-name",
        "padded",
        "prefixed-image-id",
        "short-image-id",
        "bare-image-id",
        "malformed-digest",
        "upper-case-repository",
        "empty",
    ],
)
def test_split_service_image_classifies_the_text_without_raising(
    text: str, kind: str, reference: str | None, digest: str | None
) -> None:
    image = split_service_image(text)

    assert (image.kind, image.reference, image.digest, image.text) == (
        kind,
        reference,
        digest,
        text,
    )


def test_pinned_text_keeps_the_reference_as_written_and_swaps_the_digest() -> None:
    image = split_service_image(f"{REGISTRY_IMAGE}@{DIGEST}")

    assert pinned_text(image, OTHER_DIGEST) == f"{REGISTRY_IMAGE}@{OTHER_DIGEST}"


@pytest.mark.parametrize(
    ("text", "digest"),
    [
        (REGISTRY_IMAGE, "sha256:short"),
        (REGISTRY_IMAGE, HEX),
        (REGISTRY_IMAGE, "sha256:" + "A" * 64),
        (HEX[:12], OTHER_DIGEST),
        ("", OTHER_DIGEST),
    ],
    ids=["short-digest", "no-algorithm", "upper-case-digest", "image-id", "empty-text"],
)
def test_pinned_text_refuses_a_bad_digest_and_an_image_without_a_reference(
    text: str, digest: str
) -> None:
    with pytest.raises(ValueError):
        pinned_text(split_service_image(text), digest)


class Resolver:
    def __init__(self, set_id: int | None = SET_ID) -> None:
        self.calls: list[tuple[int, str, Any, Any]] = []
        self._set_id = set_id

    async def resolve(
        self, environment_id: int, image: str, *, principal: Any, choice: Any = None
    ) -> PullCredentials:
        self.calls.append((environment_id, image, principal, choice))
        return PullCredentials(self._set_id, UNRESOLVED_AUTH, "binding")


class Client:
    def __init__(self, *, fails: bool = False) -> None:
        self.distribution = SimpleNamespace(inspect=self.inspect)
        self.looked_up: list[Any] = []
        self._fails = fails

    async def inspect(self, reference: Any, registry_auth: Any = None) -> Any:
        self.looked_up.append(registry_auth)
        if self._fails:
            raise EngineError(ENVIRONMENT_ID, "no route", status_code=NO_ROUTE_STATUS)
        return SimpleNamespace(digest=DIGEST)


def authority(seen: list[int], *, refuse: bool = False) -> AutomationAuthority:
    async def allow(*_: Any) -> None:
        return None

    async def check_set(set_id: int) -> None:
        seen.append(set_id)
        if refuse:
            raise CredentialSetUnavailable("credential_mismatch")

    return AutomationAuthority(
        actor=SubsystemActor("container-updater", 1, 1, "rule"),
        authorise=allow,
        hold=lambda *_: None,  # type: ignore[arg-type]
        lock_wait=1.0,
        authorise_read=allow,
        owner_is_administrator=True,
        authorise_credential_set=check_set,
    )


async def test_an_automation_run_resolves_without_a_principal_or_choice_and_checks_the_set() -> (
    None
):
    resolver, client = Resolver(), Client()
    checked: list[int] = []

    pinned = await pin_image(
        resolver,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        authority(checked),
        ENVIRONMENT_ID,
        REGISTRY_IMAGE,
        None,
    )

    assert resolver.calls == [(ENVIRONMENT_ID, REGISTRY_IMAGE, None, None)]
    assert checked == [SET_ID]
    assert pinned is not None and pinned.pinned
    assert pinned.image == f"{REGISTRY_IMAGE}@{DIGEST}"
    assert client.looked_up == [UNRESOLVED_AUTH]


async def test_an_automation_run_whose_set_is_refused_never_asks_the_registry() -> None:
    client = Client()
    checked: list[int] = []

    with pytest.raises(CredentialSetUnavailable):
        await pin_image(
            Resolver(),  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            authority(checked, refuse=True),
            ENVIRONMENT_ID,
            REGISTRY_IMAGE,
            None,
        )

    assert checked == [SET_ID]
    assert client.looked_up == []


async def test_an_automation_run_cannot_choose_a_credential_set() -> None:
    resolver = Resolver()

    with pytest.raises(ValueError):
        await pin_image(
            resolver,  # type: ignore[arg-type]
            Client(),  # type: ignore[arg-type]
            authority([]),
            ENVIRONMENT_ID,
            REGISTRY_IMAGE,
            object(),  # type: ignore[arg-type]
        )

    assert resolver.calls == []


async def test_an_anonymous_binding_needs_no_set_check() -> None:
    checked: list[int] = []

    await pin_image(
        Resolver(None),  # type: ignore[arg-type]
        Client(),  # type: ignore[arg-type]
        authority(checked),
        ENVIRONMENT_ID,
        REGISTRY_IMAGE,
        None,
    )

    assert checked == []


async def test_a_failed_digest_lookup_leaves_the_image_unpinned_but_a_given_digest_is_kept() -> (
    None
):
    unpinned = await pin_image(
        Resolver(),  # type: ignore[arg-type]
        Client(fails=True),  # type: ignore[arg-type]
        authority([]),
        ENVIRONMENT_ID,
        REGISTRY_IMAGE,
        None,
    )
    given = await pin_image(
        Resolver(),  # type: ignore[arg-type]
        Client(fails=True),  # type: ignore[arg-type]
        authority([]),
        ENVIRONMENT_ID,
        f"{REGISTRY_IMAGE}@{DIGEST}",
        None,
    )

    assert unpinned is not None and not unpinned.pinned and unpinned.image == REGISTRY_IMAGE
    assert given is not None and given.pinned and given.digest == DIGEST


async def test_an_image_id_is_never_pinned() -> None:
    resolver = Resolver()

    pinned = await pin_image(
        resolver,  # type: ignore[arg-type]
        Client(),  # type: ignore[arg-type]
        authority([]),
        ENVIRONMENT_ID,
        DIGEST,
        None,
    )

    assert pinned is None and resolver.calls == []


async def test_a_user_resolves_as_themselves_and_is_not_checked_unless_a_check_is_given() -> None:
    resolver, checked = Resolver(), []

    async def check(set_id: int) -> None:
        checked.append(set_id)

    await pin_image(
        resolver,  # type: ignore[arg-type]
        Client(),  # type: ignore[arg-type]
        ADMIN,
        ENVIRONMENT_ID,
        REGISTRY_IMAGE,
        None,
    )
    await pin_image(
        resolver,  # type: ignore[arg-type]
        Client(),  # type: ignore[arg-type]
        ADMIN,
        ENVIRONMENT_ID,
        REGISTRY_IMAGE,
        None,
        authorise_set=check,
    )

    assert [call[2] for call in resolver.calls] == [ADMIN, ADMIN]
    assert checked == [SET_ID]
