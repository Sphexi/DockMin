"""The Swarm pages: the gate, roles, headers, CSRF, escaping and the route set."""

import copy
import json
from collections.abc import Iterable

from fastapi import FastAPI
from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.container_support import plant_control
from tests.docker_engine.swarm_engine import WORKER, SwarmEngine, plain_spec
from tests.swarm_support import NAMESPACE_LABEL
from tests.web.environment_support import World
from tests.web.helpers import resource_tiles_in

OK, SEE_OTHER, BAD, FORBIDDEN, NOT_FOUND = 200, 303, 400, 403, 404
XSS = ("<script>alert", "<img src=x onerror", "<svg/onload", "</pre><script", '"><script')
LOG_TAILS_FOR_A_SERVICE = ["100", "200", "500", "1000", "5000"]


class Seed:
    """The ids of the objects a page test deploys."""

    def __init__(self, host: SwarmEngine) -> None:
        self.web = host.add_service(
            {
                "Name": "web",
                "Labels": {"tier": "front", "api_key": "label-secret-value"},
                "Mode": {"Replicated": {"Replicas": 2}},
                "TaskTemplate": {
                    "ContainerSpec": {
                        "Image": "busybox:latest",
                        "Args": ["sleep", "100"],
                        "Env": ["MODE=blue", "DB_PASSWORD=hunter2-env"],
                    },
                    "Placement": {"Constraints": ["node.role==worker"]},
                },
                "UpdateConfig": {"Parallelism": 1, "Delay": 10_000_000_000},
                "EndpointSpec": {
                    "Mode": "vip",
                    "Ports": [
                        {
                            "Protocol": "tcp",
                            "TargetPort": 80,
                            "PublishedPort": 8080,
                            "PublishMode": "ingress",
                        }
                    ],
                },
            }
        )
        previous = copy.deepcopy(host.services[self.web]["Spec"])
        previous["TaskTemplate"]["ContainerSpec"]["Args"] = ["sleep", "50"]
        host.services[self.web]["PreviousSpec"] = previous
        self.agent = host.add_service(
            {
                "Name": "agent",
                "Labels": {NAMESPACE_LABEL: "Shop"},
                "Mode": {"Global": {}},
                "TaskTemplate": {"ContainerSpec": {"Image": "busybox:latest"}},
            }
        )
        self.config = host.add_config("app-conf", b"line one\nline two\n<b>bold</b>")
        self.binary = host.add_config("blob", bytes(range(256)))
        self.secret = host.add_secret("db-pass")
        self.task = host.service_tasks(self.web)[0]


def paths(base: str, seed: Seed) -> dict[str, str]:
    web = f"{base}/services/{seed.web}"
    return {
        "overview": base,
        "services": f"{base}/services",
        "services filtered": f"{base}/services?q=we&mode=replicated",
        "service form": f"{base}/services/new",
        "service": web,
        "service configuration": f"{web}?tab=configuration",
        "service tasks tab": f"{web}?tab=tasks",
        "service logs": f"{web}?tab=logs",
        "service logs recent": f"{web}?tab=logs&recent=1",
        "service access": f"{web}?tab=access",
        "edit": f"{web}/edit",
        "scale": f"{web}/scale?replicas=1",
        "force update": f"{web}/force-update",
        "rollback": f"{web}/rollback",
        "remove service": f"{web}/remove",
        "task": f"{base}/tasks/{seed.task}",
        "nodes": f"{base}/nodes",
        "node": f"{base}/nodes/{WORKER}",
        "drain": f"{base}/nodes/{WORKER}/availability?availability=drain",
        "configs": f"{base}/configs",
        "config form": f"{base}/configs/new",
        "config": f"{base}/configs/{seed.config}",
        "binary config": f"{base}/configs/{seed.binary}",
        "remove config": f"{base}/configs/{seed.config}/remove",
        "secrets": f"{base}/secrets",
        "secret form": f"{base}/secrets/new",
        "secret": f"{base}/secrets/{seed.secret}",
        "secret access": f"{base}/secrets/{seed.secret}?tab=access",
        "remove secret": f"{base}/secrets/{seed.secret}/remove",
        "stacks": f"{base}/stacks",
        "stack": f"{base}/stacks/Shop",
        "stack access": f"{base}/stacks/Shop?tab=access",
    }


async def fetch(client: AsyncClient, urls: Iterable[str]) -> dict[str, Response]:
    return {url: await client.get(url) for url in urls}


async def test_every_page_renders_for_an_administrator_and_forms_and_confirmations_are_no_store(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    base = f"/environments/{world.remote_id}/swarm"
    urls = paths(base, Seed(swarm_host))

    responses = await fetch(admin_client, urls.values())

    assert {url: r.status_code for url, r in responses.items()} == dict.fromkeys(urls.values(), OK)
    no_store = {url: r.headers.get("cache-control") for url, r in responses.items()}
    assert no_store == dict.fromkeys(urls.values(), "no-store")


async def test_the_service_logs_tab_offers_tail_choices_up_to_5000_only(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    seed = Seed(swarm_host)
    base = f"/environments/{world.remote_id}"

    service = await admin_client.get(f"{base}/swarm/services/{seed.web}?tab=logs")

    for choice in LOG_TAILS_FOR_A_SERVICE:
        assert f'<option value="{choice}"' in service.text
    assert '<option value="10000"' not in service.text


async def test_the_recent_logs_view_shows_labelled_lines_in_time_order_without_scripting(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    seed = Seed(swarm_host)
    first, second = swarm_host.service_tasks(seed.web)
    swarm_host.add_log(first, "2026-01-01T00:00:01.000000000Z", "hello <b>one</b>")
    swarm_host.add_log(first, "2026-01-01T00:00:03.000000000Z", "oops", "err")
    swarm_host.add_log(second, "2026-01-01T00:00:02.000000000Z", "hello two")

    base = f"/environments/{world.remote_id}/swarm/services/{seed.web}"

    page = await admin_client.get(f"{base}?tab=logs&recent=1")
    ndjson = await admin_client.get(f"{base}/logs/download?format=ndjson")
    plain = await admin_client.get(f"{base}/logs/download")
    unknown = await admin_client.get(f"{base}/logs/download?format=xml")

    text = page.text
    assert f"web.1.{first[:12]}" in text and f"web.2.{second[:12]}" in text
    assert "hello &lt;b&gt;one&lt;/b&gt;" in text and "<b>one</b>" not in text
    assert text.index("hello &lt;b&gt;one") < text.index("hello two") < text.index("oops")
    records = [json.loads(line) for line in ndjson.text.split("\n")[:-1]]
    assert ndjson.status_code == OK
    assert ndjson.headers["content-type"] == "application/x-ndjson"
    assert ndjson.headers["content-disposition"].startswith('attachment; filename="web-')
    assert ndjson.headers["content-disposition"].endswith('.ndjson"')
    assert [(r["message"], r["stream"], r["task_id"]) for r in records] == [
        ("hello <b>one</b>", "stdout", first),
        ("hello two", "stdout", second),
        ("oops", "stderr", first),
    ]
    assert all(r["schema_version"] == 2 for r in records)
    assert all(r["format"] == "text" and r["service_name"] == "web" for r in records)
    assert plain.headers["content-type"] == "text/plain; charset=utf-8"
    assert plain.headers["content-disposition"].endswith('.log"')
    assert unknown.status_code == BAD
    assert unknown.text == "Choose text or ndjson."


async def test_the_environment_detail_links_to_swarm_only_on_a_manager_and_notes_a_worker(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    worker = await admin_client.get(f"/environments/{world.local_id}")
    manager = await admin_client.get(f"/environments/{world.remote_id}")
    standard = await standard_client.get(f"/environments/{world.remote_id}")

    assert (
        "Swarm worker: Swarm objects are managed through a manager of this cluster." in worker.text
    )
    assert f"/environments/{world.local_id}/swarm" not in worker.text
    assert f'<a class="nav__link" href="/environments/{world.remote_id}/swarm">' in manager.text
    assert f'<a class="nav__link" href="/environments/{world.remote_id}/swarm">' in standard.text
    assert "/swarm/nodes" not in standard.text


async def test_literal_paths_resolve_to_their_pages_and_not_to_an_id_route(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    base = f"/environments/{world.remote_id}/swarm"
    literal = [
        "/services/new",
        "/configs/new",
        "/secrets/new",
        "/nodes",
        "/stacks",
        "",
        "/services",
    ]

    responses = await fetch(admin_client, [base + path for path in literal])

    assert {r.status_code for r in responses.values()} == {OK}


async def test_no_swarm_path_resolves_on_a_worker_or_a_never_contacted_environment(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
) -> None:
    seed = Seed(swarm_host)
    swarm_host.requests.clear()
    results: dict[str, int] = {}
    for env_id in (world.local_id, world.down_id):
        for client in (admin_client, standard_client):
            for name, url in paths(f"/environments/{env_id}/swarm", seed).items():
                results[f"{env_id} {id(client)} {name}"] = (await client.get(url)).status_code
            for url in (
                f"/environments/{env_id}/swarm/services/new",
                f"/environments/{env_id}/swarm/configs/new",
                f"/environments/{env_id}/swarm/nodes/{WORKER}/availability",
                f"/environments/{env_id}/swarm/services/{seed.web}/scale",
            ):
                post = await client.post(url, data=csrf_form(client, name="x"))
                results[f"{env_id} {id(client)} POST {url}"] = post.status_code

    assert set(results.values()) == {NOT_FOUND}
    assert swarm_host.requests == []


MALFORMED = [
    "services/short",
    "services/" + "S" * 25,
    "tasks/x",
    "nodes/" + "a" * 26,
    "configs/%2e%2e",
    "stacks/-bad",
    "stacks/" + "a" * 70,
]


async def test_a_malformed_id_is_not_found_for_every_role_before_any_engine_call(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
) -> None:
    swarm_host.requests.clear()
    urls = [f"/environments/{world.remote_id}/swarm/{tail}" for tail in MALFORMED]

    admin = await fetch(admin_client, urls)
    standard = await fetch(standard_client, urls)

    assert {r.status_code for r in (*admin.values(), *standard.values())} == {NOT_FOUND}
    assert swarm_host.requests == []


async def test_the_swarm_overview_shows_counts_as_tiles_that_link_to_their_lists(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    Seed(swarm_host)
    base = f"/environments/{world.remote_id}/swarm"

    page = await admin_client.get(base)

    tiles = {t.label: t for t in resource_tiles_in(page.text)}
    assert tiles["Services"].href == f"{base}/services"
    assert tiles["Configs"].href == f"{base}/configs"
    assert tiles["Secret"].href == f"{base}/secrets"
    assert tiles["Nodes"].href == f"{base}/nodes"
    assert tiles["Tasks running"].href is None
    assert tiles["Tasks running"].element == "div"
    assert tiles["Manager"].href is None
    assert all(t.count for t in tiles.values())


async def test_a_standard_users_swarm_overview_tiles_are_labelled_as_what_they_can_see(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    Seed(swarm_host)

    page = await standard_client.get(f"/environments/{world.remote_id}/swarm")

    labels = [t.label for t in resource_tiles_in(page.text)]
    assert labels == [
        "Services you can see",
        "Tasks running",
        "Configs you can see",
        "Secrets you can see",
    ]


async def test_a_standard_user_with_no_grant_sees_an_empty_swarm_and_no_cluster_facts(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    seed = Seed(swarm_host)
    base = f"/environments/{world.remote_id}/swarm"

    services = await standard_client.get(f"{base}/services")
    service = await standard_client.get(f"{base}/services/{seed.web}")
    overview = await standard_client.get(base)
    nodes = await standard_client.get(f"{base}/nodes")
    configs = await standard_client.get(f"{base}/configs")

    assert services.status_code == OK and "web" not in services.text.split("<main")[1]
    assert service.status_code == NOT_FOUND
    assert overview.status_code == OK and "Cluster ID" not in overview.text
    assert nodes.status_code in (FORBIDDEN, NOT_FOUND)
    assert configs.status_code == OK and "No configs" in configs.text


async def test_read_access_shows_a_service_without_edit_controls_and_hides_the_access_tab(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine, started_app: FastAPI
) -> None:
    seed = Seed(swarm_host)
    base = f"/environments/{world.remote_id}/swarm"
    await plant_control(started_app, world.remote_id, "service", seed.web, public=True)

    listing = await standard_client.get(f"{base}/services")
    detail = await standard_client.get(f"{base}/services/{seed.web}")
    tasks = await standard_client.get(f"{base}/services/{seed.web}?tab=tasks")
    results = [
        await standard_client.get(f"{base}/services/{seed.web}{suffix}")
        for suffix in ("/edit", "/remove", "/scale?replicas=1")
    ]
    access = await standard_client.get(f"{base}/services/{seed.web}?tab=access")
    node = await standard_client.get(f"{base}/nodes/{WORKER}")

    assert "web" in listing.text.split("<main")[1]
    assert (
        detail.status_code == OK
        and "Edit" not in detail.text.split("service-summary")[1].split("</section>")[0]
    )
    assert "mgr" in tasks.text and f"/environments/{world.remote_id}/swarm/nodes" not in tasks.text
    assert [r.status_code for r in results] == [FORBIDDEN] * 3
    assert access.status_code in (FORBIDDEN, NOT_FOUND) and node.status_code in (
        FORBIDDEN,
        NOT_FOUND,
    )


async def test_a_standard_user_may_open_the_task_of_a_service_they_can_read(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine, started_app: FastAPI
) -> None:
    seed = Seed(swarm_host)
    base = f"/environments/{world.remote_id}/swarm"
    hidden = await standard_client.get(f"{base}/tasks/{seed.task}")
    await plant_control(started_app, world.remote_id, "service", seed.web, public=True)

    shown = await standard_client.get(f"{base}/tasks/{seed.task}")

    assert (hidden.status_code, shown.status_code) == (NOT_FOUND, OK)


async def test_on_a_local_socket_manager_every_swarm_write_is_forbidden_to_a_standard_user(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine, started_app: FastAPI
) -> None:
    async with started_app.state.database.session() as db:
        await db.execute(
            "UPDATE environment SET type = 'swarm', swarm_state = 'active', "
            "swarm_control_available = 1 WHERE id = ?",
            (world.local_id,),
        )
    base = f"/environments/{world.local_id}/swarm"
    web = swarm_host.add_service(plain_spec("web"))
    secret = swarm_host.add_secret("s1")
    await plant_control(started_app, world.local_id, "service", web, public=True, level="write")
    await plant_control(started_app, world.local_id, "secret", secret, public=True, level="write")
    version = str(swarm_host.services[web]["Version"]["Index"])
    swarm_host.requests.clear()

    posts = {
        "new service": (f"{base}/services/new", {"name": "n", "image": "busybox:latest"}),
        "scale": (
            f"{base}/services/{web}/scale",
            {"replicas": "3", "expected_version": version, "expected_replicas": "1"},
        ),
        "force": (f"{base}/services/{web}/force-update", {"expected_version": version}),
        "rollback": (f"{base}/services/{web}/rollback", {"expected_version": version}),
        "remove": (
            f"{base}/services/{web}/remove",
            {"expected_name": "web", "expected_version": version},
        ),
        "remove secret": (
            f"{base}/secrets/{secret}/remove",
            {"expected_name": "s1", "expected_in_use": "0"},
        ),
        "new secret": (f"{base}/secrets/new", {"name": "s2", "value": "v"}),
        "new config": (f"{base}/configs/new", {"name": "c2", "content": "v"}),
    }
    codes = {
        name: (await standard_client.post(url, data=csrf_form(standard_client, **data))).status_code
        for name, (url, data) in posts.items()
    }
    edit = await standard_client.get(f"{base}/services/{web}/edit")

    assert set(codes.values()) == {FORBIDDEN} and edit.status_code == FORBIDDEN
    assert [r for r in swarm_host.requests if r.method != "GET"] == []


async def test_every_swarm_post_without_a_csrf_token_is_refused_and_changes_nothing(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    seed = Seed(swarm_host)
    base = f"/environments/{world.remote_id}/swarm"
    version = str(swarm_host.services[seed.web]["Version"]["Index"])
    posts = [
        (f"{base}/services/new", {"name": "x", "image": "y"}),
        (f"{base}/services/{seed.web}/edit", {"image": "y"}),
        (f"{base}/services/{seed.web}/edit/confirm", {"expected_version": version}),
        (f"{base}/services/{seed.web}/scale", {"replicas": "3", "expected_version": version}),
        (f"{base}/services/{seed.web}/force-update", {"expected_version": version}),
        (f"{base}/services/{seed.web}/rollback", {"expected_version": version}),
        (f"{base}/services/{seed.web}/remove", {"expected_name": "web"}),
        (f"{base}/services/{seed.web}/access", {"visibility": "public"}),
        (f"{base}/services/{seed.web}/access/delete", {}),
        (f"{base}/nodes/{WORKER}/availability", {"availability": "drain"}),
        (f"{base}/configs/new", {"name": "c", "content": "v"}),
        (f"{base}/configs/{seed.config}/remove", {"expected_name": "app-conf"}),
        (f"{base}/secrets/new", {"name": "s", "value": "v"}),
        (f"{base}/secrets/{seed.secret}/remove", {"expected_name": "db-pass"}),
        (f"{base}/stacks/Shop/access", {"visibility": "public"}),
        (f"{base}/stacks/Shop/access/delete", {}),
    ]
    swarm_host.requests.clear()

    codes = [(await admin_client.post(url, data=data)).status_code for url, data in posts]

    assert codes == [FORBIDDEN] * len(posts)
    assert [r for r in swarm_host.requests if r.method != "GET"] == []


async def test_daemon_supplied_text_is_escaped_on_every_page(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    swarm_host.nodes[WORKER]["Description"]["Hostname"] = "<svg/onload=alert(1)>"
    swarm_host.nodes[WORKER]["Spec"]["Labels"] = {"k": "<script>alert(9)</script>"}
    evil = swarm_host.add_service(
        {
            "Name": "<script>alert(1)</script>",
            "Labels": {
                "x": "<img src=x onerror=alert(1)>",
                NAMESPACE_LABEL: "<script>alert(5)</script>",
            },
            "Mode": {"Replicated": {"Replicas": 2}},
            "TaskTemplate": {
                "ContainerSpec": {
                    "Image": 'busybox:latest"><script>alert(4)</script>',
                    "Args": ["<script>alert(6)</script>"],
                    "Env": ["A=<script>alert(7)</script>"],
                    "Mounts": [
                        {"Type": "volume", "Source": "<svg/onload=alert(1)>", "Target": "/x"}
                    ],
                },
                "Placement": {"Constraints": ["node.labels.x==<script>alert(8)</script>"]},
            },
        }
    )
    swarm_host.services[evil]["UpdateStatus"] = {
        "State": "paused",
        "Message": "<script>alert(10)</script>",
    }
    for task in swarm_host.service_tasks(evil):
        swarm_host.tasks[task]["Status"]["Err"] = "<script>alert(2)</script>"
        swarm_host.add_log(task, "2026-01-01T00:00:01.000000000Z", "<script>alert(11)</script>")
    config = swarm_host.add_config(
        "conf", b"</pre><script>alert(3)</script>", labels={"l": "<script>alert(12)</script>"}
    )
    base = f"/environments/{world.remote_id}"
    task = swarm_host.service_tasks(evil)[0]
    urls = [
        f"{base}/swarm",
        f"{base}/swarm/services",
        f"{base}/swarm/services/{evil}",
        f"{base}/swarm/services/{evil}?tab=configuration",
        f"{base}/swarm/services/{evil}?tab=tasks",
        f"{base}/swarm/services/{evil}?tab=logs&recent=1",
        f"{base}/swarm/services/{evil}/edit",
        f"{base}/swarm/services/{evil}/remove",
        f"{base}/swarm/services/{evil}/force-update",
        f"{base}/swarm/tasks/{task}",
        f"{base}/swarm/nodes",
        f"{base}/swarm/nodes/{WORKER}",
        f"{base}/swarm/nodes/{WORKER}/availability?availability=drain",
        f"{base}/swarm/configs",
        f"{base}/swarm/configs/{config}",
        f"{base}/swarm/stacks",
        f"{base}/networks",
    ]

    responses = await fetch(admin_client, urls)

    assert {r.status_code for r in responses.values()} == {OK}
    leaked = {url: [p for p in XSS if p in r.text] for url, r in responses.items()}
    assert not any(leaked.values()), leaked


async def test_a_hostile_service_id_in_a_list_neither_breaks_the_list_nor_the_network_pages(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    swarm_host.add_service(plain_spec("ok"))
    evil = swarm_host.add_service(plain_spec("evil"))
    record = swarm_host.services.pop(evil)
    record["ID"] = "evil/../../../users"
    swarm_host.services[record["ID"]] = record
    base = f"/environments/{world.remote_id}"

    swarm = await admin_client.get(f"{base}/swarm/services")
    networks = await admin_client.get(f"{base}/networks")

    assert swarm.status_code == OK and "ok" in swarm.text and "evil" not in swarm.text
    assert networks.status_code == OK


EXPECTED_ROUTES = {
    "": ["get"],
    "/configs": ["get"],
    "/configs/new": ["get", "post"],
    "/configs/{object_id}": ["get"],
    "/configs/{object_id}/access": ["post"],
    "/configs/{object_id}/access/delete": ["post"],
    "/configs/{object_id}/remove": ["get", "post"],
    "/nodes": ["get"],
    "/nodes/{node_id}": ["get"],
    "/nodes/{node_id}/availability": ["get", "post"],
    "/secrets": ["get"],
    "/secrets/new": ["get", "post"],
    "/secrets/{object_id}": ["get"],
    "/secrets/{object_id}/access": ["post"],
    "/secrets/{object_id}/access/delete": ["post"],
    "/secrets/{object_id}/remove": ["get", "post"],
    "/services": ["get"],
    "/services/new": ["get", "post"],
    "/services/new/choices": ["post"],
    "/services/{service_id}": ["get"],
    "/services/{service_id}/access": ["post"],
    "/services/{service_id}/access/delete": ["post"],
    "/services/{service_id}/edit": ["get", "post"],
    "/services/{service_id}/edit/confirm": ["post"],
    "/services/{service_id}/force-update": ["get", "post"],
    "/services/{service_id}/logs/download": ["get"],
    "/services/{service_id}/logs/stream": ["get"],
    "/services/{service_id}/remove": ["get", "post"],
    "/services/{service_id}/rollback": ["get", "post"],
    "/services/{service_id}/scale": ["get", "post"],
    "/services/{service_id}/tasks": ["get"],
    "/stacks": ["get"],
    "/stacks/-/adopt/{namespace}": ["get", "post"],
    "/stacks/-/adopt/{namespace}/validate": ["post"],
    "/stacks/-/adopt/{namespace}/read": ["post"],
    "/stacks/-/adopt/{namespace}/as-is": ["post"],
    "/stacks/-/unmanaged/{namespace}/dismiss": ["post"],
    "/stacks/-/unmanaged/{namespace}/restore": ["post"],
    "/stacks/-/jobs/{job_id}": ["get"],
    "/stacks/-/jobs/{job_id}/events": ["get"],
    "/stacks/-/new": ["get", "post"],
    "/stacks/-/validate": ["post"],
    "/stacks/{namespace}": ["get"],
    "/stacks/{namespace}/access": ["post"],
    "/stacks/{namespace}/access/delete": ["post"],
    "/stacks/{namespace}/edit": ["get", "post"],
    "/stacks/{namespace}/files/-/new": ["get", "post"],
    "/stacks/{namespace}/files/{file}/edit": ["get", "post"],
    "/stacks/{namespace}/files/{file}/remove": ["get", "post"],
    "/stacks/{namespace}/forget": ["get", "post"],
    "/stacks/{namespace}/logs/download": ["get"],
    "/stacks/{namespace}/logs/stream": ["get"],
    "/stacks/{namespace}/redeploy": ["get", "post"],
    "/stacks/{namespace}/redeploy/review": ["post"],
    "/stacks/{namespace}/remove": ["get", "post"],
    "/stacks/{namespace}/validate": ["post"],
    "/stacks/{namespace}/variables/{variable_id}": ["get"],
    "/stacks/{namespace}/variables/{variable_id}/reveal": ["post"],
    "/tasks/{task_id}": ["get"],
}


def test_the_swarm_route_set_is_exactly_the_documented_one(app: FastAPI) -> None:
    prefix = "/environments/{environment_id}/swarm"

    found = {
        path.removeprefix(prefix): sorted(methods)
        for path, methods in app.openapi()["paths"].items()
        if path.startswith(prefix)
    }

    assert found == EXPECTED_ROUTES
