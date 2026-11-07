"""Resolving a credential set into a pull login, and keeping derived secrets fresh."""

import ast
import asyncio
import base64
import json
import logging
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.auth.principal import Principal
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.registry_access import RegistryAccessRepository
from app.registry_client.ecr import DerivedSecretSource
from app.registry_client.errors import TokenExchangeKind
from app.registry_client.providers import PROVIDERS
from app.services.activity import ActivityRecorder, audited
from app.services.assembly import build_registry_resolver
from app.services.pull_credentials import PullCredentials
from app.services.registry_resolution import (
    FAILURE_BACKOFF,
    CredentialSetUnavailable,
    DerivedSecretCache,
    RegistryCredentialResolver,
)
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.auth_support import STANDARD_ID
from tests.registry_support import (
    ADDRESS,
    ECR_ADDRESS,
    ISSUED_PASSWORD,
    ISSUED_USERNAME,
    SECRET,
    USERNAME,
    RegistryRig,
    ScriptedDeriver,
    add_anonymous,
    add_ecr,
    add_static,
    ecr_fields,
    grant_environment,
    grant_team,
    grant_user,
    make_team,
    raw_row,
)

RESOLVER_SOURCE = Path(__file__).resolve().parents[2] / "app/services/registry_resolution.py"
HOUR = timedelta(hours=1)
LIFETIME = timedelta(hours=12)
ABSENT_ID = 9999
PROVIDER_NAMES = frozenset(PROVIDERS)


def decoded(pull: PullCredentials) -> dict[str, str]:
    assert pull.registry_auth is not None
    auth: dict[str, str] = json.loads(
        base64.urlsafe_b64decode(pull.registry_auth.get_secret_value())
    )
    return auth


def password_of(pull: PullCredentials) -> str:
    return decoded(pull)["password"]


async def resolve(
    rig: RegistryRig,
    credential_id: int,
    *,
    environment_id: int = ENV_A,
    principal: Principal | None = ADMIN,
) -> PullCredentials:
    return await rig.services.resolver.resolve_pull_credentials(
        credential_id, environment_id=environment_id, principal=principal
    )


async def ready_ecr(rig: RegistryRig, name: str = "ecr") -> int:
    stored = await add_ecr(rig, name)
    await grant_environment(rig.db, stored.id, ENV_A)
    return stored.id


async def ready_static(rig: RegistryRig, name: str = "registry", **overrides: Any) -> int:
    stored = await add_static(rig, name, **overrides)
    await grant_environment(rig.db, stored.id, ENV_A)
    return stored.id


async def resolve_with(
    rig: RegistryRig, derivers: Mapping[str, DerivedSecretSource], credential_id: int
) -> PullCredentials:
    """Resolve through a resolver whose derived-secret cache dispatches to the given mapping."""
    cache = DerivedSecretCache(
        database=rig.database, vault=rig.vault, derivers=derivers, clock=rig.clock
    )
    resolver = RegistryCredentialResolver(
        credentials=rig.credentials,
        access=RegistryAccessRepository(rig.db),
        environments=EnvironmentAccessRepository(rig.db),
        cache=cache,
    )
    return await resolver.resolve_pull_credentials(
        credential_id, environment_id=ENV_A, principal=ADMIN
    )


async def settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def test_a_static_set_resolves_to_the_header_of_its_user_name_secret_and_address(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)

    pull = await resolve(registry_rig, credential_id)

    assert pull.credential_set_id == credential_id
    assert decoded(pull) == {"username": USERNAME, "password": SECRET, "serveraddress": ADDRESS}


async def test_docker_hub_resolves_with_the_index_address_the_engine_expects(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(
        registry_rig, "hub", provider="dockerhub", url="docker.io", username="hubuser"
    )

    pull = await resolve(registry_rig, credential_id)

    assert decoded(pull)["serveraddress"] == "https://index.docker.io/v1/"


async def test_an_anonymous_set_resolves_with_its_id_and_no_header(
    registry_rig: RegistryRig,
) -> None:
    stored = await add_anonymous(registry_rig)
    await grant_environment(registry_rig.db, stored.id, ENV_A)

    pull = await resolve(registry_rig, stored.id)

    assert (pull.credential_set_id, pull.registry_auth) == (stored.id, None)


async def test_a_pull_login_hides_its_header_in_a_repr(registry_rig: RegistryRig) -> None:
    pull = await resolve(registry_rig, await ready_static(registry_rig))

    assert [s for s in (SECRET, USERNAME) if s in repr(pull)] == []


async def test_the_first_resolve_of_a_derived_set_exchanges_and_stores_the_token_encrypted(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)
    started = registry_rig.clock.now

    pull = await resolve(registry_rig, credential_id)

    row = await raw_row(registry_rig.db, credential_id)
    issued_password = f"{ISSUED_PASSWORD}-1"
    assert decoded(pull) == {
        "username": ISSUED_USERNAME,
        "password": issued_password,
        "serveraddress": ECR_ADDRESS,
    }
    assert issued_password not in json.dumps(row, default=str)
    assert row["derived_secret_ciphertext"] not in (None, "")
    assert row["derived_secret_expires_at"] == (started + LIFETIME).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert row["derived_secret_refreshed_at"] == started.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert [r.username for r in registry_rig.deriver.requests] == ["AKIAIOSFODNN7EXAMPLE"]


async def test_a_cached_token_is_read_back_from_the_database_without_a_new_exchange(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)
    first = await resolve(registry_rig, credential_id)
    registry_rig.clock.advance(timedelta(hours=2))

    second = await resolve(registry_rig, credential_id)

    assert password_of(second) == password_of(first)
    assert registry_rig.deriver.issued == 1


@pytest.mark.parametrize(
    ("elapsed", "exchanges"),
    [
        (timedelta(hours=10, minutes=59), 1),
        (timedelta(hours=11), 2),
        (timedelta(hours=11, minutes=1), 2),
        (timedelta(hours=12), 2),
        (timedelta(hours=13), 2),
    ],
    ids=["just-outside-margin", "exactly-at-margin", "just-inside-margin", "at-expiry", "expired"],
)
async def test_a_token_is_refreshed_once_its_remaining_life_falls_to_the_one_hour_margin(
    registry_rig: RegistryRig, elapsed: timedelta, exchanges: int
) -> None:
    credential_id = await ready_ecr(registry_rig)
    await resolve(registry_rig, credential_id)
    registry_rig.clock.advance(elapsed)

    await resolve(registry_rig, credential_id)

    assert registry_rig.deriver.issued == exchanges


@pytest.mark.parametrize(
    ("min_remaining", "elapsed", "exchanges"),
    [
        (None, timedelta(hours=10, minutes=59), 1),
        (None, timedelta(hours=11), 2),
        (timedelta(hours=3), timedelta(hours=8, minutes=59), 1),
        (timedelta(hours=3), timedelta(hours=9), 2),
        (timedelta(minutes=10), timedelta(hours=10, minutes=59), 1),
        (timedelta(minutes=10), timedelta(hours=11), 2),
    ],
    ids=[
        "none-outside-margin",
        "none-at-margin",
        "wider-outside",
        "wider-at-threshold",
        "narrower-keeps-margin-outside",
        "narrower-keeps-margin-at",
    ],
)
async def test_a_min_remaining_longer_than_the_margin_widens_it_and_a_shorter_one_changes_nothing(
    registry_rig: RegistryRig, min_remaining: timedelta | None, elapsed: timedelta, exchanges: int
) -> None:
    credential_id = await ready_ecr(registry_rig)
    await resolve(registry_rig, credential_id)
    registry_rig.clock.advance(elapsed)

    await registry_rig.services.resolver.resolve_pull_credentials(
        credential_id, environment_id=ENV_A, principal=ADMIN, min_remaining=min_remaining
    )

    assert registry_rig.deriver.issued == exchanges


@pytest.mark.parametrize(
    ("elapsed", "exchanges"),
    [(timedelta(minutes=59), 1), (timedelta(hours=1), 2)],
    ids=["life-left-above-cap", "life-left-at-cap"],
)
async def test_a_request_longer_than_a_tokens_lifetime_is_capped_at_lifetime_less_the_margin(
    registry_rig: RegistryRig, elapsed: timedelta, exchanges: int
) -> None:
    beyond_lifetime = LIFETIME * 2
    credential_id = await ready_ecr(registry_rig)
    await registry_rig.services.resolver.resolve_pull_credentials(
        credential_id, environment_id=ENV_A, principal=ADMIN, min_remaining=beyond_lifetime
    )
    registry_rig.clock.advance(elapsed)

    await registry_rig.services.resolver.resolve_pull_credentials(
        credential_id, environment_id=ENV_A, principal=ADMIN, min_remaining=beyond_lifetime
    )

    assert registry_rig.deriver.issued == exchanges


@pytest.mark.parametrize(
    ("elapsed", "exchanges"),
    [(timedelta(minutes=14), 1), (timedelta(minutes=15), 2)],
    ids=["outside-half-lifetime", "at-half-lifetime"],
)
async def test_a_short_lived_token_is_refreshed_at_half_its_lifetime_not_at_a_fixed_hour(
    registry_rig: RegistryRig, elapsed: timedelta, exchanges: int
) -> None:
    registry_rig.deriver.lifetime = timedelta(minutes=30)
    credential_id = await ready_ecr(registry_rig)
    await resolve(registry_rig, credential_id)
    registry_rig.clock.advance(elapsed)

    await resolve(registry_rig, credential_id)

    assert registry_rig.deriver.issued == exchanges


async def test_a_token_claiming_a_longer_life_than_twelve_hours_is_stored_with_twelve(
    registry_rig: RegistryRig,
) -> None:
    registry_rig.deriver.lifetime = timedelta(days=30)
    credential_id = await ready_ecr(registry_rig)
    started = registry_rig.clock.now

    await resolve(registry_rig, credential_id)

    row = await raw_row(registry_rig.db, credential_id)
    assert row["derived_secret_expires_at"] == (started + LIFETIME).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_concurrent_resolves_of_one_set_make_one_exchange(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)
    registry_rig.deriver.gate = asyncio.Event()
    async with registry_rig.database.session() as other:
        second = build_registry_resolver(other, registry_rig.runtime, registry_rig.registries)
        first_call = asyncio.create_task(resolve(registry_rig, credential_id))
        second_call = asyncio.create_task(
            second.resolve_pull_credentials(credential_id, environment_id=ENV_A, principal=ADMIN)
        )
        await settle()
        registry_rig.deriver.gate.set()

        first, other_pull = await asyncio.gather(first_call, second_call)

    assert registry_rig.deriver.issued == 1
    assert password_of(first) == password_of(other_pull)


async def test_a_failed_exchange_is_unavailable_and_later_resolves_fail_fast_for_sixty_seconds(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)
    registry_rig.deriver.fail_with(TokenExchangeKind.DENIED)

    with pytest.raises(CredentialSetUnavailable) as first:
        await resolve(registry_rig, credential_id)
    registry_rig.clock.advance(FAILURE_BACKOFF - timedelta(seconds=1))
    with pytest.raises(CredentialSetUnavailable):
        await resolve(registry_rig, credential_id)
    during = len(registry_rig.deriver.requests)
    registry_rig.clock.advance(timedelta(seconds=1))
    with pytest.raises(CredentialSetUnavailable):
        await resolve(registry_rig, credential_id)

    assert first.value.reason == "token_exchange_failed"
    assert "The exchange failed." in str(first.value)
    assert (during, len(registry_rig.deriver.requests)) == (1, 2)


async def test_a_token_that_is_still_valid_is_used_when_its_refresh_fails(
    registry_rig: RegistryRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    credential_id = await ready_ecr(registry_rig)
    first = await resolve(registry_rig, credential_id)
    registry_rig.clock.advance(LIFETIME - timedelta(minutes=30))
    registry_rig.deriver.fail_with(TokenExchangeKind.UNREACHABLE)

    pull = await resolve(registry_rig, credential_id)

    assert password_of(pull) == password_of(first)
    failures = [
        r for r in caplog.records if r.getMessage() == "registry_derived_secret_refresh_failed"
    ]
    assert [(r.__dict__["credential_set_id"], r.__dict__["kind"]) for r in failures] == [
        (credential_id, "unreachable")
    ]


async def test_an_expired_token_whose_refresh_fails_is_unavailable(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)
    await resolve(registry_rig, credential_id)
    registry_rig.clock.advance(LIFETIME + timedelta(minutes=1))
    registry_rig.deriver.fail_with(TokenExchangeKind.CLOCK_SKEW)

    with pytest.raises(CredentialSetUnavailable) as unavailable:
        await resolve(registry_rig, credential_id)

    assert unavailable.value.reason == "token_exchange_failed"


async def test_an_exchange_that_issues_an_already_expired_token_is_unusable(
    registry_rig: RegistryRig,
) -> None:
    registry_rig.deriver.lifetime = -HOUR
    credential_id = await ready_ecr(registry_rig)

    with pytest.raises(CredentialSetUnavailable, match="cannot be used"):
        await resolve(registry_rig, credential_id)

    row = await raw_row(registry_rig.db, credential_id)
    assert row["derived_secret_ciphertext"] is None


async def test_an_exchange_that_crashes_is_unavailable_and_the_message_is_product_written(
    registry_rig: RegistryRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    registry_rig.deriver.failure = RuntimeError(f"boom {SECRET}")
    credential_id = await ready_ecr(registry_rig)

    with pytest.raises(CredentialSetUnavailable) as unavailable:
        await resolve(registry_rig, credential_id)

    logs = " ".join(f"{r.getMessage()} {r.__dict__!r}" for r in caplog.records)
    assert SECRET not in str(unavailable.value) + logs


async def test_an_edit_made_during_an_exchange_discards_the_token_and_the_exchange_runs_again(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)
    edits = 0

    async def re_key_once() -> None:
        nonlocal edits
        edits += 1
        if edits == 1:
            await registry_rig.credentials.update_replacing_secret(
                credential_id, ecr_fields(), SecretStr("a-newly-entered-secret")
            )

    registry_rig.deriver.before_return = re_key_once

    pull = await resolve(registry_rig, credential_id)

    row = await raw_row(registry_rig.db, credential_id)
    assert registry_rig.deriver.issued == 2
    assert password_of(pull) == f"{ISSUED_PASSWORD}-2"
    assert row["derived_secret_ciphertext"] is not None
    assert registry_rig.deriver.requests[1].secret.get_secret_value() == "a-newly-entered-secret"


async def test_a_set_that_keeps_changing_under_the_exchange_is_unavailable_after_one_retry(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)
    counter = 0

    async def re_key_every_time() -> None:
        nonlocal counter
        counter += 1
        await registry_rig.credentials.update_replacing_secret(
            credential_id, ecr_fields(), SecretStr(f"secret-{counter}")
        )

    registry_rig.deriver.before_return = re_key_every_time

    with pytest.raises(CredentialSetUnavailable) as unavailable:
        await resolve(registry_rig, credential_id)

    row = await raw_row(registry_rig.db, credential_id)
    assert unavailable.value.reason == "token_exchange_failed"
    assert registry_rig.deriver.issued == 2
    assert row["derived_secret_ciphertext"] is None


async def test_a_cached_token_that_cannot_be_opened_is_replaced_by_a_new_exchange(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)
    await resolve(registry_rig, credential_id)
    await registry_rig.db.execute(
        "UPDATE registry_credential SET derived_secret_ciphertext = 'v1:garbage' WHERE id = ?",
        (credential_id,),
    )

    pull = await resolve(registry_rig, credential_id)

    assert password_of(pull) == f"{ISSUED_PASSWORD}-2"


async def test_a_derived_set_with_no_exchange_registered_for_its_provider_is_unavailable(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)

    with pytest.raises(CredentialSetUnavailable, match="No credential exchange is available"):
        await resolve_with(registry_rig, {}, credential_id)

    assert registry_rig.deriver.requests == []


class Operation:
    """An audited operation that resolves credentials and then commits its own write."""

    def __init__(self, rig: RegistryRig) -> None:
        self._activity = ActivityRecorder(rig.db, rig.clock)
        self._rig = rig

    @audited(action="registry_credential_updated", target_type="registry_credential")
    async def pull_then_write(self, actor: Principal, credential_id: int) -> PullCredentials:
        pull = await resolve(self._rig, credential_id)
        async with self._rig.db.transaction():
            await self._rig.db.execute(
                "UPDATE registry_credential SET name = name || '!' WHERE id = ?", (credential_id,)
            )
        return pull


async def test_a_refresh_inside_an_audited_operation_commits_on_its_own_session(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)

    pull = await Operation(registry_rig).pull_then_write(ADMIN, credential_id)

    row = await raw_row(registry_rig.db, credential_id)
    assert row["derived_secret_ciphertext"] is not None
    assert row["name"].endswith("!")
    assert password_of(pull) == f"{ISSUED_PASSWORD}-1"


async def test_resolving_inside_a_write_transaction_is_refused_before_anything_is_exchanged(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)

    async with registry_rig.db.transaction():
        with pytest.raises(RuntimeError, match="outside a write transaction"):
            await resolve(registry_rig, credential_id)

    assert registry_rig.deriver.requests == []


async def test_an_unreadable_long_lived_secret_is_reported_as_such(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)
    await registry_rig.db.execute(
        "UPDATE registry_credential SET secret_ciphertext = 'v1:garbage' WHERE id = ?",
        (credential_id,),
    )

    with pytest.raises(CredentialSetUnavailable) as unavailable:
        await resolve(registry_rig, credential_id)

    assert unavailable.value.reason == "secret_unreadable"


async def test_a_set_that_does_not_exist_or_is_not_granted_reads_alike(
    registry_rig: RegistryRig,
) -> None:
    ungranted = (await add_static(registry_rig, "ungranted")).id

    with pytest.raises(CredentialSetUnavailable) as absent:
        await resolve(registry_rig, ABSENT_ID)
    with pytest.raises(CredentialSetUnavailable) as not_granted:
        await resolve(registry_rig, ungranted)

    assert (absent.value.reason, not_granted.value.reason) == ("missing", "not_granted_environment")
    assert str(absent.value) == str(not_granted.value)


async def test_a_set_granted_to_another_environment_is_not_usable_on_this_one(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)

    with pytest.raises(CredentialSetUnavailable) as unavailable:
        await resolve(registry_rig, credential_id, environment_id=ENV_B)

    assert unavailable.value.reason == "not_granted_environment"


async def test_revoking_the_environment_grant_refuses_the_next_resolve(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)
    await resolve(registry_rig, credential_id)

    await registry_rig.services.credentials.revoke_environment(ADMIN, credential_id, ENV_A)

    with pytest.raises(CredentialSetUnavailable):
        await resolve(registry_rig, credential_id)


async def test_a_standard_user_needs_the_environment_and_a_grant_on_the_set(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)

    with pytest.raises(CredentialSetUnavailable) as without:
        await resolve(registry_rig, credential_id, principal=STANDARD)
    await grant_user(registry_rig.db, credential_id, STANDARD_ID)
    granted = await resolve(registry_rig, credential_id, principal=STANDARD)

    assert without.value.reason == "not_granted_user"
    assert granted.credential_set_id == credential_id


async def test_a_team_grant_is_enough_for_a_member_and_revoking_it_refuses_at_once(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)
    team = await make_team(registry_rig.db, "platform", STANDARD_ID)
    await grant_team(registry_rig.db, credential_id, team)
    await resolve(registry_rig, credential_id, principal=STANDARD)

    await registry_rig.services.credentials.revoke(ADMIN, credential_id, team_id=team)

    with pytest.raises(CredentialSetUnavailable) as unavailable:
        await resolve(registry_rig, credential_id, principal=STANDARD)
    assert unavailable.value.reason == "not_granted_user"


async def test_a_standard_user_who_cannot_see_the_environment_is_refused_even_with_both_grants(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)
    await grant_user(registry_rig.db, credential_id, STANDARD_ID)
    await EnvironmentAccessRepository(registry_rig.db).remove_user(
        "environment", ENV_A, STANDARD_ID
    )

    with pytest.raises(CredentialSetUnavailable) as unavailable:
        await resolve(registry_rig, credential_id, principal=STANDARD)

    assert unavailable.value.reason == "not_granted_environment"


async def test_an_administrator_needs_only_the_environment_grant(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)

    pull = await resolve(registry_rig, credential_id, principal=ADMIN)

    assert pull.credential_set_id == credential_id


async def test_the_automation_path_without_a_principal_passes_on_the_environment_grant_alone(
    registry_rig: RegistryRig,
) -> None:
    granted = await ready_static(registry_rig)
    ungranted = (await add_static(registry_rig, "ungranted", username="other")).id

    pull = await resolve(registry_rig, granted, principal=None)
    with pytest.raises(CredentialSetUnavailable):
        await resolve(registry_rig, ungranted, principal=None)

    assert pull.credential_set_id == granted


def resolver_tree() -> ast.Module:
    return ast.parse(RESOLVER_SOURCE.read_text(encoding="utf-8"))


def provider_branches(tree: ast.AST) -> list[int]:
    """Return the line of every provider literal and every comparison or match on a provider."""
    return [
        cast("ast.expr | ast.stmt", node).lineno
        for node in ast.walk(tree)
        if _is_provider_branch(node)
    ]


def _is_provider_branch(node: ast.AST) -> bool:
    match node:
        case ast.Constant(value=str() as text):
            return text in PROVIDER_NAMES
        case ast.Compare():
            return any(_names_provider(operand) for operand in _operands(node))
        case ast.Match():
            return _names_provider(node.subject)
    return False


def _operands(node: ast.Compare) -> list[ast.expr]:
    return [node.left, *node.comparators]


def _names_provider(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "provider") or (
        isinstance(node, ast.Name) and node.id == "provider"
    )


@pytest.mark.parametrize(
    "source",
    [
        'if stored.provider == "x": pass',
        'if "ecr" in names: pass',
        "match row.provider:\n    case _: pass",
        "if provider != other: pass",
        "if stored.provider in (a, b): pass",
    ],
)
def test_the_provider_scanner_flags_every_form_of_provider_branching(source: str) -> None:
    assert provider_branches(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source",
    [
        "deriver = derivers.get(stored.provider)",
        "if stored.secret_kind == 'derived': pass",
        "address = server_address(stored.provider, stored.url)",
    ],
)
def test_the_provider_scanner_allows_dispatch_by_mapping_and_by_secret_kind(source: str) -> None:
    assert provider_branches(ast.parse(source)) == []


def test_the_pull_path_resolver_contains_no_provider_branch() -> None:
    assert provider_branches(resolver_tree()) == []


async def test_a_derived_set_is_exchanged_by_whichever_deriver_is_registered_for_its_provider(
    registry_rig: RegistryRig,
) -> None:
    replacement = ScriptedDeriver(registry_rig.clock, password="replacement")
    credential_id = await ready_ecr(registry_rig)

    pull = await resolve_with(registry_rig, {"ecr": replacement}, credential_id)

    assert password_of(pull) == "replacement-1"
    assert registry_rig.deriver.requests == []


async def login_of(
    rig: RegistryRig,
    credential_id: int,
    *,
    environment_id: int = ENV_A,
    principal: Principal | None = ADMIN,
    address: str | None = None,
) -> Any:
    return await rig.services.resolver.resolve_login(
        credential_id,
        environment_id=environment_id,
        principal=principal,
        registry_address=address,
    )


async def test_a_static_set_resolves_to_its_target_and_login(registry_rig: RegistryRig) -> None:
    credential_id = await ready_static(registry_rig)

    resolved = await login_of(registry_rig, credential_id)

    assert resolved.target.address == ADDRESS
    assert resolved.target.verify_tls is True
    assert (resolved.login.username, resolved.login.secret.get_secret_value()) == (USERNAME, SECRET)


async def test_an_anonymous_set_resolves_to_its_target_and_no_login(
    registry_rig: RegistryRig,
) -> None:
    stored = await add_anonymous(registry_rig)
    await grant_environment(registry_rig.db, stored.id, ENV_A)

    resolved = await login_of(registry_rig, stored.id)

    assert resolved.login is None
    assert resolved.target.address == ADDRESS


async def test_a_derived_set_resolves_to_the_issued_login_through_the_cache(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)

    resolved = await login_of(registry_rig, credential_id)

    assert resolved.login.username == ISSUED_USERNAME
    assert resolved.login.secret.get_secret_value().startswith(ISSUED_PASSWORD)
    assert len(registry_rig.deriver.requests) == 1


async def test_resolving_a_login_inside_a_write_transaction_is_refused(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)

    async with registry_rig.db.transaction():
        with pytest.raises(RuntimeError, match="outside a write transaction"):
            await login_of(registry_rig, credential_id)

    assert registry_rig.deriver.requests == []


@pytest.mark.parametrize(
    ("principal", "environment_id", "reason"),
    [
        pytest.param(STANDARD, ENV_A, "not_granted_user", id="user-without-a-grant"),
        pytest.param(ADMIN, ENV_B, "not_granted_environment", id="environment-not-granted"),
    ],
)
async def test_a_login_is_resolved_under_the_same_grants_as_a_pull(
    registry_rig: RegistryRig, principal: Principal, environment_id: int, reason: str
) -> None:
    credential_id = await ready_static(registry_rig)

    with pytest.raises(CredentialSetUnavailable) as refused:
        await login_of(
            registry_rig, credential_id, principal=principal, environment_id=environment_id
        )

    assert refused.value.reason == reason


async def test_a_login_is_resolved_for_a_standard_user_who_holds_a_grant(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)
    await grant_user(registry_rig.db, credential_id, STANDARD_ID)

    resolved = await login_of(registry_rig, credential_id, principal=STANDARD)

    assert resolved.login.username == USERNAME


@pytest.mark.parametrize("resolve_what", ["pull", "login"])
async def test_a_set_at_another_address_than_asked_for_is_refused_before_a_secret_is_read(
    registry_rig: RegistryRig, resolve_what: str
) -> None:
    credential_id = await ready_ecr(registry_rig)
    resolver = registry_rig.services.resolver
    attempts = {
        "pull": resolver.resolve_pull_credentials(
            credential_id, environment_id=ENV_A, principal=ADMIN, registry_address="other.example"
        ),
        "login": resolver.resolve_login(
            credential_id, environment_id=ENV_A, principal=ADMIN, registry_address="other.example"
        ),
    }

    with pytest.raises(CredentialSetUnavailable) as refused:
        await attempts.pop(resolve_what)
    for unused in attempts.values():
        unused.close()

    assert refused.value.reason == "credential_mismatch"
    assert registry_rig.deriver.requests == []


async def test_a_set_at_the_address_asked_for_is_resolved(registry_rig: RegistryRig) -> None:
    credential_id = await ready_static(registry_rig)

    resolved = await login_of(registry_rig, credential_id, address=ADDRESS)

    assert resolved.target.address == ADDRESS


async def check_set(
    rig: RegistryRig,
    credential_id: int,
    *,
    environment_id: int = ENV_A,
    principal: Principal | None = None,
    address: str | None = None,
) -> None:
    await rig.services.resolver.check_pull_set(
        credential_id,
        environment_id=environment_id,
        principal=principal,
        registry_address=address,
    )


async def test_a_granted_set_passes_the_pull_checks_without_a_secret_being_read_or_exchanged(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_ecr(registry_rig)

    await check_set(registry_rig, credential_id, address=ECR_ADDRESS)
    await check_set(registry_rig, credential_id, principal=ADMIN)

    assert registry_rig.deriver.requests == []


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        ("absent", "missing"),
        ("ungranted", "not_granted_environment"),
        ("other_environment", "not_granted_environment"),
        ("other_address", "credential_mismatch"),
    ],
)
async def test_a_set_that_would_fail_a_pull_fails_the_check_for_the_same_reason(
    registry_rig: RegistryRig, prepare: str, reason: str
) -> None:
    ungranted = (await add_static(registry_rig, "ungranted")).id
    granted = await ready_static(registry_rig)
    attempts = {
        "absent": check_set(registry_rig, ABSENT_ID),
        "ungranted": check_set(registry_rig, ungranted),
        "other_environment": check_set(registry_rig, granted, environment_id=ENV_B),
        "other_address": check_set(registry_rig, granted, address="other.example"),
    }

    with pytest.raises(CredentialSetUnavailable) as refused:
        await attempts.pop(prepare)
    for unused in attempts.values():
        unused.close()

    assert refused.value.reason == reason


async def test_a_standard_user_needs_a_grant_on_the_set_for_the_check_as_for_a_pull(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)

    with pytest.raises(CredentialSetUnavailable) as without:
        await check_set(registry_rig, credential_id, principal=STANDARD)
    await grant_user(registry_rig.db, credential_id, STANDARD_ID)
    await check_set(registry_rig, credential_id, principal=STANDARD)

    assert without.value.reason == "not_granted_user"


async def test_the_check_is_refused_inside_a_write_transaction_like_a_pull(
    registry_rig: RegistryRig,
) -> None:
    credential_id = await ready_static(registry_rig)

    async with registry_rig.db.transaction():
        with pytest.raises(RuntimeError):
            await check_set(registry_rig, credential_id)
