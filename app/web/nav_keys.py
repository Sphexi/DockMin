"""The sidebar item a page belongs to, from its route name."""

from typing import Final

# The first prefix that matches wins, so a longer prefix precedes any shorter one it starts with.
ROUTE_PREFIXES: Final = (
    ("environment_swarm_overview", "swarm"),
    ("environment_swarm_service", "services"),
    ("environment_swarm_task", "services"),
    ("environment_swarm_node", "nodes"),
    ("environment_swarm_config", "configs"),
    ("environment_swarm_secret", "secrets"),
    ("environment_swarm_stack", "swarm-stacks"),
    ("environment_swarm", "swarm"),
    ("environment_container", "containers"),
    ("environment_image", "images"),
    ("environment_volume", "volumes"),
    ("environment_network", "networks"),
    ("environment_stack", "stacks"),
    ("environment_new", "environments"),
    ("environment_create", "environments"),
    ("environment_preview", "environments"),
    ("environment_delete", "environments"),
    ("environment_", "dashboard"),
    ("index", "environments"),
    ("group", "groups"),
    ("tag", "tags"),
    ("registr", "registries"),
    ("job", "jobs"),
    ("activity", "activity"),
    ("object_storage", "storage"),
    ("template_catalogue", "template-catalogues"),
    ("template", "templates"),
    ("notification", "notifications"),
    ("backup", "backups"),
    ("restore", "backups"),
    ("server_certificate", "certificate"),
    ("server_encryption", "encryption"),
    ("server_instance", "instance"),
    ("automation", "automation"),
    ("team", "teams"),
    ("user", "users"),
    ("ui_kit", "ui-kit"),
    ("account", "account"),
    ("token", "tokens"),
)


def navigation_key(route_name: str) -> str:
    """Return the sidebar key of a route, or an empty string for a page outside the sidebar."""
    return next((key for prefix, key in ROUTE_PREFIXES if route_name.startswith(prefix)), "")
