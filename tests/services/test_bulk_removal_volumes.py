"""Removing several volumes at once, pruning, and the volumes dockmin's own container holds."""

import pytest

from app.auth.errors import ResourceNotFound, SelfProtected, ValidationFailed
from app.services.assembly import VolumeServices, build_volume_services
from app.services.container_actions import MAX_BATCH
from app.services.volume_views import VolumeBatchEntry, VolumeBatchTarget, VolumePruneTarget
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.docker_engine.container_engine import ContainerEngine
from tests.object_support import add_container, rows_of, volume_mount
from tests.self_support import (
    SELF_ID,
    SELF_NAME,
    SELF_VOLUME,
    Held,
    home_identity,
    self_protected_rows,
)
from tests.services.conftest import ContainerRig

CREATED_AT = "2024-05-06T07:08:09Z"
OTHER_CREATED_AT = "2024-06-01T00:00:00Z"
USER_NAME = "user"
SELF_REFUSAL = str(SelfProtected.volume(SELF_VOLUME))


@pytest.fixture
def catalogue(host: ContainerEngine) -> ContainerEngine:
    """Two free volumes, one a container uses, and the one dockmin's own container mounts."""
    host.add_volume("data")
    host.add_volume("cache")
    host.add_volume("busy")
    host.add_volume(SELF_VOLUME)
    add_container(host, 1, USER_NAME, mounts=[volume_mount("busy")])
    add_container(host, 0x5E1F, SELF_NAME, state="running", mounts=[volume_mount(SELF_VOLUME)])
    return host


@pytest.fixture
def guarded(rig: ContainerRig, catalogue: ContainerEngine) -> VolumeServices:
    return build_volume_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine.as_runtime(),
        rig.access,
        rig.inspector,
        rig.recorder,
        self_identity=Held(home_identity(volumes=(SELF_VOLUME,))),
    )


def reviewed(entry: VolumeBatchEntry) -> VolumeBatchTarget:
    return VolumeBatchTarget(entry.name, entry.created_at_text, entry.in_use)


def removed_volumes(host: ContainerEngine) -> list[str]:
    return [name for name, _ in host.volume_removals()]


class TestBatchRemoval:
    async def test_the_confirmation_names_each_volume_its_users_and_what_is_kept(
        self, guarded: VolumeServices
    ) -> None:
        names = ["data", "busy", SELF_VOLUME, "missing"]

        review = await guarded.queries.batch_confirmation(ADMIN, ENV_A, names)

        by_name = {t.name: t for t in review.targets}
        assert list(by_name) == ["data", "busy"]
        assert by_name["data"].in_use == 0
        assert by_name["busy"].in_use == 1
        assert [c.name for c in by_name["busy"].containers] == [USER_NAME]
        assert [k.name for k in review.kept] == [SELF_VOLUME]
        assert review.unavailable == 1

    async def test_the_run_removes_the_reviewed_volumes_and_reports_one_in_use_without_removing_it(
        self, rig: ContainerRig, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        review = await guarded.queries.batch_confirmation(ADMIN, ENV_A, ["data", "busy", "cache"])

        result = await guarded.actions.run_batch_remove(
            ADMIN, ENV_A, [reviewed(t) for t in review.targets]
        )

        assert [(i.name, i.status) for i in result.items] == [
            ("data", "removed"),
            ("busy", "in_use"),
            ("cache", "removed"),
        ]
        assert removed_volumes(catalogue) == ["data", "cache"]
        rows = await rows_of(rig.db, "volume_removed")
        assert [(r["target_id"], r["result"], r["detail"]["batch"]) for r in rows] == [
            ("data", "success", True),
            ("busy", "failure", True),
            ("cache", "success", True),
        ]

    async def test_a_volume_recreated_since_the_review_is_reported_changed_and_left_alone(
        self, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        target = VolumeBatchTarget("data", OTHER_CREATED_AT, 0)

        result = await guarded.actions.run_batch_remove(ADMIN, ENV_A, [target])

        assert [i.status for i in result.items] == ["changed"]
        assert removed_volumes(catalogue) == []

    async def test_a_volume_mounted_since_the_review_is_reported_changed(
        self, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        review = await guarded.queries.batch_confirmation(ADMIN, ENV_A, ["data"])
        add_container(catalogue, 2, "late", mounts=[volume_mount("data")])

        result = await guarded.actions.run_batch_remove(
            ADMIN, ENV_A, [reviewed(t) for t in review.targets]
        )

        assert [i.status for i in result.items] == ["changed"]
        assert removed_volumes(catalogue) == []

    async def test_a_volume_removed_since_the_review_is_reported_gone(
        self, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        review = await guarded.queries.batch_confirmation(ADMIN, ENV_A, ["data"])
        del catalogue.volumes["data"]

        result = await guarded.actions.run_batch_remove(
            ADMIN, ENV_A, [reviewed(t) for t in review.targets]
        )

        assert [i.status for i in result.items] == ["gone"]

    @pytest.mark.parametrize("count", [0, MAX_BATCH + 1])
    async def test_a_selection_that_is_empty_or_over_the_cap_is_refused_before_anything_runs(
        self, guarded: VolumeServices, catalogue: ContainerEngine, count: int
    ) -> None:
        names = [f"volume-{n}" for n in range(count)]
        targets = [VolumeBatchTarget(name, CREATED_AT, 0) for name in names]

        with pytest.raises(ValidationFailed) as on_review:
            await guarded.queries.batch_confirmation(ADMIN, ENV_A, names)
        with pytest.raises(ValidationFailed) as on_run:
            await guarded.actions.run_batch_remove(ADMIN, ENV_A, targets)

        assert "volumes" in on_review.value.fields
        assert "volumes" in on_run.value.fields
        assert removed_volumes(catalogue) == []

    async def test_a_repeated_volume_is_removed_once(
        self, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        target = VolumeBatchTarget("data", CREATED_AT, 0)

        result = await guarded.actions.run_batch_remove(ADMIN, ENV_A, [target, target])

        assert [i.status for i in result.items] == ["removed"]
        assert removed_volumes(catalogue) == ["data"]


class TestSelfVolume:
    async def test_the_batch_reports_the_volume_dockmin_mounts_as_protected_and_sends_nothing(
        self, rig: ContainerRig, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        targets = [
            VolumeBatchTarget(SELF_VOLUME, CREATED_AT, 1),
            VolumeBatchTarget("data", CREATED_AT, 0),
        ]

        result = await guarded.actions.run_batch_remove(ADMIN, ENV_A, targets)

        assert [(i.name, i.status) for i in result.items] == [
            (SELF_VOLUME, "protected"),
            ("data", "removed"),
        ]
        assert result.items[0].message == SELF_REFUSAL
        assert removed_volumes(catalogue) == ["data"]
        [row] = await self_protected_rows(rig.db)
        assert (row["action"], row["target_id"]) == ("volume_removed", SELF_VOLUME)

    async def test_removing_the_volume_dockmin_mounts_is_refused_with_a_row_and_no_engine_call(
        self, rig: ContainerRig, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        with pytest.raises(SelfProtected) as refused:
            await guarded.actions.remove(
                ADMIN, ENV_A, SELF_VOLUME, expected_created_at=CREATED_AT, expected_in_use=1
            )

        [row] = await self_protected_rows(rig.db)
        assert str(refused.value) == SELF_REFUSAL
        assert row["detail"]["message"] == SELF_REFUSAL
        assert removed_volumes(catalogue) == []
        assert catalogue.mutations() == []

    async def test_the_force_path_refuses_the_volume_before_removing_any_container(
        self, rig: ContainerRig, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        with pytest.raises(SelfProtected):
            await guarded.actions.remove_with_containers(
                ADMIN,
                ENV_A,
                SELF_VOLUME,
                expected_created_at=CREATED_AT,
                expected_containers="digest",
                expected_names={SELF_ID: SELF_NAME},
            )

        [row] = await self_protected_rows(rig.db)
        assert (row["action"], row["target_id"]) == ("volume_removed", SELF_VOLUME)
        assert catalogue.mutations() == []
        assert SELF_ID in catalogue.boxes

    async def test_a_user_who_cannot_see_the_volume_gets_the_not_found_any_hidden_volume_gives(
        self, rig: ContainerRig, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        with pytest.raises(ResourceNotFound) as hidden:
            await guarded.actions.remove(
                STANDARD, ENV_A, "cache", expected_created_at=CREATED_AT, expected_in_use=0
            )
        with pytest.raises(ResourceNotFound) as protected:
            await guarded.actions.remove(
                STANDARD, ENV_A, SELF_VOLUME, expected_created_at=CREATED_AT, expected_in_use=1
            )

        assert str(protected.value) == str(hidden.value)
        assert await self_protected_rows(rig.db) == []

    async def test_the_prune_plan_leaves_the_volume_out_and_lists_it_as_kept(
        self, guarded: VolumeServices
    ) -> None:
        plan = await guarded.queries.prune_plan(ADMIN, ENV_A, "all")

        assert [c.name for c in plan.candidates] == ["cache", "data"]
        assert [k.name for k in plan.kept] == [SELF_VOLUME]

    async def test_a_prune_run_keeps_the_volume_even_when_it_is_named_in_the_selection(
        self, rig: ContainerRig, guarded: VolumeServices, catalogue: ContainerEngine
    ) -> None:
        targets = [
            VolumePruneTarget(SELF_VOLUME, CREATED_AT),
            VolumePruneTarget("data", CREATED_AT),
        ]

        result = await guarded.actions.run_prune(ADMIN, ENV_A, "all", targets)

        assert [(i.name, i.status) for i in result.items] == [
            (SELF_VOLUME, "protected"),
            ("data", "removed"),
        ]
        assert removed_volumes(catalogue) == ["data"]
        assert await self_protected_rows(rig.db) == []

    async def test_the_volume_queries_flag_the_volume_as_protected(
        self, guarded: VolumeServices
    ) -> None:
        listing = await guarded.queries.page(ADMIN, ENV_A)
        detail = await guarded.queries.detail(ADMIN, ENV_A, SELF_VOLUME)
        impact = await guarded.queries.remove_impact(ADMIN, ENV_A, SELF_VOLUME)

        flagged = {row.name: row.protected for row in listing.page.items}
        assert flagged[SELF_VOLUME] is True
        assert flagged["data"] is False
        assert detail.protected is True
        assert impact.protected is True
