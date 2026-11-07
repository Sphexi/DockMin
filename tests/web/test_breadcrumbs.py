"""The breadcrumb trail every page renders: where it sits, and which crumbs link."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from typing import Literal

import pytest
from httpx import AsyncClient

from tests.auth_support import csrf_form
from tests.web.html_tree import Node, Page
from tests.web.page_world import PAGES, ROLES, Key, world_pages, world_paths  # noqa: F401

pytestmark = pytest.mark.xdist_group("page-world")

Kind = Literal["list", "detail", "under", "create"]

OK = 200
ENVIRONMENT_HREF = re.compile(r"/environments/\d+")
HOSTILE_NAME = "<b>bold</b>"


@dataclass(frozen=True)
class Crumbs:
    kind: Kind
    resource: str
    suffix: str
    in_environment: bool = False


def top(kind: Kind, resource: str, suffix: str) -> Crumbs:
    return Crumbs(kind, resource, suffix)


def scoped(kind: Kind, resource: str, suffix: str) -> Crumbs:
    return Crumbs(kind, resource, suffix, in_environment=True)


ENVIRONMENTS = ("Environments", "/")
CONTAINERS = ("Containers", "/containers")
IMAGES = ("Images", "/images")
NETWORKS = ("Networks", "/networks")
VOLUMES = ("Volumes", "/volumes")
STACKS = ("Stacks", "/stacks")
SERVICES = ("Services", "/swarm/services")
NODES = ("Nodes", "/swarm/nodes")
CONFIGS = ("Configs", "/swarm/configs")
SWARM_STACKS = ("Swarm stacks", "/swarm/stacks")
AUTOMATION = ("Automation", "/automation")
JOBS = ("Jobs", "/jobs")
BACKUPS = ("Backups", "/backups")
REGISTRIES = ("Registries", "/registries")
STORAGE = ("Object storage", "/object-storage")
ACTIVITY = ("Activity", "/activity")
USERS = ("Users", "/users")
TEAMS = ("Teams", "/teams")
GROUPS = ("Groups", "/groups")
TEMPLATES = ("Templates", "/templates")

EXPECTED: dict[str, Crumbs] = {
    "dashboard": top("list", *ENVIRONMENTS),
    "account": top("list", "Account", "/account"),
    "tokens": top("list", "API tokens", "/account/tokens"),
    "ui-kit": top("list", "UI kit", "/ui-kit"),
    "environment-new": top("create", *ENVIRONMENTS),
    "environment-detail": top("detail", *ENVIRONMENTS),
    "environment-edit": top("under", *ENVIRONMENTS),
    "environment-unreachable": top("detail", *ENVIRONMENTS),
    "container-list": scoped("list", *CONTAINERS),
    "container-detail": scoped("detail", *CONTAINERS),
    "container-new": scoped("create", *CONTAINERS),
    "container-new-template": scoped("create", *CONTAINERS),
    "container-stop-confirm": scoped("under", *CONTAINERS),
    "container-remove-confirm": scoped("under", *CONTAINERS),
    "container-remove-force-confirm": scoped("under", *CONTAINERS),
    "container-batch-confirm": scoped("create", *CONTAINERS),
    "container-batch-force-confirm": scoped("create", *CONTAINERS),
    "container-recreate-confirm": scoped("under", *CONTAINERS),
    "container-connect": scoped("under", *CONTAINERS),
    "container-networks": scoped("detail", *CONTAINERS),
    "container-stats": scoped("detail", *CONTAINERS),
    "container-automation": scoped("detail", *CONTAINERS),
    "container-access": scoped("detail", *CONTAINERS),
    "container-terminal": scoped("under", *CONTAINERS),
    "image-list": scoped("list", *IMAGES),
    "image-detail": scoped("detail", *IMAGES),
    "image-pull": scoped("create", *IMAGES),
    "image-remove-confirm": scoped("under", *IMAGES),
    "image-remove-force-confirm": scoped("under", *IMAGES),
    "image-untag-confirm": scoped("under", *IMAGES),
    "image-prune-confirm": scoped("create", *IMAGES),
    "image-batch-confirm": scoped("create", *IMAGES),
    "network-list": scoped("list", *NETWORKS),
    "network-detail": scoped("detail", *NETWORKS),
    "network-new": scoped("create", *NETWORKS),
    "network-remove-confirm": scoped("under", *NETWORKS),
    "volume-list": scoped("list", *VOLUMES),
    "volume-detail": scoped("detail", *VOLUMES),
    "volume-new": scoped("create", *VOLUMES),
    "volume-remove-confirm": scoped("under", *VOLUMES),
    "volume-remove-force-confirm": scoped("under", *VOLUMES),
    "volume-prune-confirm": scoped("create", *VOLUMES),
    "volume-batch-confirm": scoped("create", *VOLUMES),
    "stack-list": scoped("list", *STACKS),
    "stack-new": scoped("create", *STACKS),
    "stack-new-template": scoped("create", *STACKS),
    "stack-adopt": scoped("create", *STACKS),
    "swarm-overview": scoped("list", "Swarm", "/swarm"),
    "swarm-service-list": scoped("list", *SERVICES),
    "swarm-service-detail": scoped("detail", *SERVICES),
    "swarm-service-automation": scoped("detail", *SERVICES),
    "swarm-service-access": scoped("detail", *SERVICES),
    "swarm-service-new": scoped("create", *SERVICES),
    "swarm-service-scale-confirm": scoped("under", *SERVICES),
    "swarm-node-list": scoped("list", *NODES),
    "swarm-node-detail": scoped("detail", *NODES),
    "swarm-config-list": scoped("list", *CONFIGS),
    "swarm-config-detail": scoped("detail", *CONFIGS),
    "swarm-secret-list": scoped("list", "Secrets", "/swarm/secrets"),
    "swarm-stack-list": scoped("list", *SWARM_STACKS),
    "swarm-stack-adopt": scoped("create", *SWARM_STACKS),
    "swarm-task-detail": scoped("under", *SERVICES),
    "template-list": top("list", *TEMPLATES),
    "template-detail": top("detail", *TEMPLATES),
    "registry-list": top("list", *REGISTRIES),
    "registry-new": top("create", *REGISTRIES),
    "registry-detail": top("detail", *REGISTRIES),
    "object-storage-list": top("list", *STORAGE),
    "object-storage-new": top("create", *STORAGE),
    "backup-list": top("list", *BACKUPS),
    "restore": top("create", *BACKUPS),
    "server-certificate": top("list", "Certificate", "/server/certificate"),
    "job-list": top("list", *JOBS),
    "job-detail": top("detail", *JOBS),
    "job-subsystems": top("create", *JOBS),
    "job-run-detail": top("under", *JOBS),
    "automation-overview": top("list", *AUTOMATION),
    "automation-new": top("create", *AUTOMATION),
    "automation-rule-detail": top("detail", *AUTOMATION),
    "automation-rule-edit": top("under", *AUTOMATION),
    "automation-rule-enable": top("under", *AUTOMATION),
    "automation-rule-run": top("under", *AUTOMATION),
    "automation-rule-delete": top("under", *AUTOMATION),
    "automation-run-detail": top("under", *AUTOMATION),
    "automation-webhook-new": top("create", *AUTOMATION),
    "automation-webhook-detail": top("detail", *AUTOMATION),
    "automation-webhook-edit": top("under", *AUTOMATION),
    "automation-webhook-enable": top("under", *AUTOMATION),
    "automation-webhook-run": top("under", *AUTOMATION),
    "automation-webhook-delete": top("under", *AUTOMATION),
    "automation-webhook-rotate-confirm": top("under", *AUTOMATION),
    "automation-webhook-revoke-confirm": top("under", *AUTOMATION),
    "instance": top("list", "Instance", "/server/instance"),
    "instance-restart-confirm": top("create", "Instance", "/server/instance"),
    "activity-list": top("list", *ACTIVITY),
    "activity-detail": top("detail", *ACTIVITY),
    "user-list": top("list", *USERS),
    "user-new": top("create", *USERS),
    "user-detail": top("detail", *USERS),
    "team-list": top("list", *TEAMS),
    "team-new": top("create", *TEAMS),
    "team-detail": top("detail", *TEAMS),
    "group-list": top("list", *GROUPS),
    "group-new": top("create", *GROUPS),
    "group-detail": top("detail", *GROUPS),
    "tag-list": top("list", "Tags", "/tags"),
}
UNCLASSIFIED = frozenset({"not-found"})
OBJECT_OUTSIDE_PATH = {
    "swarm-task-detail": re.compile(r"/environments/\d+/swarm/services/[^/?]+\?tab=tasks"),
    "job-run-detail": re.compile(r"/jobs/\d+"),
}
SHAPES: dict[Kind, tuple[bool, ...]] = {
    "list": (False,),
    "detail": (True, False),
    "under": (True, True, False),
    "create": (True, False),
}


@dataclass(frozen=True)
class Crumb:
    text: str
    href: str | None
    current: bool


def trail_of(page: Page) -> list[Crumb]:
    navs = [n for n in page.tags("nav") if n.attrs.get("aria-label") == "Breadcrumb"]
    assert len(navs) == 1
    return [crumb_of(item) for item in navs[0].walk() if item.tag == "li"]


def crumb_of(item: Node) -> Crumb:
    inner = next(n for n in item.walk() if n.tag in ("a", "span"))
    return Crumb(inner.text(), inner.attrs.get("href"), inner.attrs.get("aria-current") == "page")


def served(pages: dict[Key, Page]) -> list[tuple[Key, Page]]:
    return [(key, page) for key, page in pages.items() if page.status == OK]


def problems(pages: dict[Key, Page], check: Callable[[str, list[Crumb]], list[str]]) -> list[str]:
    found: list[str] = []
    for (role, label), page in served(pages):
        found += [f"{role}/{label}: {text}" for text in check(label, trail_of(page))]
    return found


def test_every_page_kind_is_classified_for_the_trail_it_must_render() -> None:
    assert set(EXPECTED) == set(PAGES) - UNCLASSIFIED


def test_both_roles_are_checked_on_the_pages_that_answer_ok(pages: dict[Key, Page]) -> None:
    served_keys = {key for key, _ in served(pages)}

    assert {role for role, _ in served_keys} == set(ROLES)
    assert {label for _, label in served_keys} >= set(EXPECTED)


def test_each_page_kind_has_the_crumbs_the_table_gives_it(pages: dict[Key, Page]) -> None:
    def check(label: str, trail: list[Crumb]) -> list[str]:
        expected = EXPECTED[label]
        prefix = (True, True) if expected.in_environment else ()
        shape = tuple(c.href is not None for c in trail)
        wanted = prefix + SHAPES[expected.kind]
        return [] if shape == wanted else [f"links {shape}, wanted {wanted}"]

    assert problems(pages, check) == []


def test_only_the_last_crumb_is_current(pages: dict[Key, Page]) -> None:
    def check(label: str, trail: list[Crumb]) -> list[str]:
        current = [i for i, crumb in enumerate(trail) if crumb.current]
        return [] if current == [len(trail) - 1] else [f"current crumbs at {current}"]

    assert problems(pages, check) == []


def test_the_resource_crumb_names_the_resource_and_links_to_its_list_on_every_page_but_a_list(
    pages: dict[Key, Page],
) -> None:
    def check(label: str, trail: list[Crumb]) -> list[str]:
        expected = EXPECTED[label]
        start = 2 if expected.in_environment else 0
        resource = trail[start]
        base = trail[1].href if expected.in_environment else ""
        want_href = None if expected.kind == "list" else f"{base}{expected.suffix}"
        wrong = []
        if resource.text != expected.resource:
            wrong.append(f"resource crumb {resource.text!r}")
        if resource.href != want_href:
            wrong.append(f"resource href {resource.href!r}, wanted {want_href!r}")
        return wrong

    assert problems(pages, check) == []


def test_a_page_inside_an_environment_starts_with_the_environments_crumb_and_the_host(
    pages: dict[Key, Page],
) -> None:
    def check(label: str, trail: list[Crumb]) -> list[str]:
        if not EXPECTED[label].in_environment:
            return []
        first, host = trail[0], trail[1]
        wrong = [] if (first.text, first.href) == ("Environments", "/") else ["first crumb"]
        if host.href is None or not ENVIRONMENT_HREF.fullmatch(host.href) or not host.text:
            wrong.append(f"host crumb {host}")
        return wrong

    assert problems(pages, check) == []


def test_the_object_crumb_of_a_page_under_an_object_links_to_that_object(
    pages: dict[Key, Page], page_paths: dict[Key, str]
) -> None:
    offenders: list[str] = []
    for (role, label), page in served(pages):
        expected = EXPECTED[label]
        if expected.kind != "under" or label in OBJECT_OUTSIDE_PATH:
            continue
        obj = trail_of(page)[-2]
        path = page_paths[(role, label)]
        if obj.href is None or not path.startswith(obj.href.split("?")[0] + "/"):
            offenders.append(f"{role}/{label}: object href {obj.href!r} is not above {path}")

    assert offenders == []


def test_a_page_whose_address_is_not_under_its_object_still_links_to_that_object(
    pages: dict[Key, Page],
) -> None:
    linked = {label: trail_of(pages[("admin", label)])[-2].href for label in OBJECT_OUTSIDE_PATH}

    assert {
        label: pattern.fullmatch(linked[label] or "") is not None
        for label, pattern in OBJECT_OUTSIDE_PATH.items()
    } == dict.fromkeys(OBJECT_OUTSIDE_PATH, True)


def test_every_crumb_has_text(pages: dict[Key, Page]) -> None:
    def check(label: str, trail: list[Crumb]) -> list[str]:
        return [f"empty crumb at {i}" for i, crumb in enumerate(trail) if not crumb.text]

    assert problems(pages, check) == []


async def test_an_object_name_is_escaped_in_the_crumb(admin_client: AsyncClient) -> None:
    created = await admin_client.post("/teams", data=csrf_form(admin_client, name=HOSTILE_NAME))
    detail = await admin_client.get(created.headers["location"])

    crumb = detail.text.split('aria-label="Breadcrumb"', 1)[1].split("</nav>", 1)[0]
    assert HOSTILE_NAME not in crumb
    assert unescape(crumb).count(HOSTILE_NAME) == 1
