"""Size bound on the label maps of Engine documents, and the dropping of secret values."""

from typing import Any

LABEL_BUDGET_CHARS = 64 * 1024
KEPT_LABEL_KEYS = frozenset(
    {"com.docker.compose.project", "com.docker.stack.namespace", "com.docker.swarm.service.id"}
)
TRUNCATED_FIELD = "LabelsTruncated"


def budget_labels(labels: Any) -> tuple[Any, bool]:
    """Keep the stack-membership and Swarm service labels and as many others as fit the budget.

    Returns the labels to use and whether any were dropped.
    """
    if not isinstance(labels, dict):
        return labels, False
    kept: dict[Any, Any] = {
        key: value
        for key, value in labels.items()
        if isinstance(key, str) and key.casefold() in KEPT_LABEL_KEYS
    }
    used = 0
    dropped = False
    for key, value in labels.items():
        if key in kept:
            continue
        size = (len(key) if isinstance(key, str) else 0) + (
            len(value) if isinstance(value, str) else 0
        )
        if used + size > LABEL_BUDGET_CHARS:
            dropped = True
            continue
        used += size
        kept[key] = value
    return kept, dropped


def _budget_each(document: Any) -> Any:
    if not isinstance(document, list):
        return document
    for item in document:
        if isinstance(item, dict):
            item["Labels"], item[TRUNCATED_FIELD] = budget_labels(item.get("Labels"))
    return document


def budget_summaries(document: Any) -> Any:
    """Apply the label budget to each entry of a container list document."""
    return _budget_each(document)


def budget_networks(document: Any) -> Any:
    """Apply the label budget to each entry of a network list document."""
    return _budget_each(document)


def budget_volumes(document: Any) -> Any:
    """Apply the label budget to each entry of a volume list's `Volumes` array."""
    return _budget_each(document)


def budget_inspect(document: Any) -> Any:
    """Apply the label budget to the labels of a container inspect document."""
    if not isinstance(document, dict):
        return document
    config = document.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    trimmed = False
    if isinstance(config, dict):
        config["Labels"], trimmed = budget_labels(labels)
    document[TRUNCATED_FIELD] = trimmed
    return document


def _budget_service_spec(spec: Any) -> None:
    if not isinstance(spec, dict):
        return
    spec["Labels"], trimmed = budget_labels(spec.get("Labels"))
    template = spec.get("TaskTemplate")
    container = template.get("ContainerSpec") if isinstance(template, dict) else None
    if isinstance(container, dict) and "Labels" in container:
        container["Labels"], container_trimmed = budget_labels(container["Labels"])
        trimmed = trimmed or container_trimmed
    spec[TRUNCATED_FIELD] = trimmed


def budget_service(document: Any) -> Any:
    """Apply the label budget to a service's spec and previous spec, and to their container labels.

    A cut in either map of a spec is reported once, on that spec.
    """
    if isinstance(document, dict):
        _budget_service_spec(document.get("Spec"))
        _budget_service_spec(document.get("PreviousSpec"))
    return document


def budget_services(document: Any) -> Any:
    """Apply `budget_service` to each entry of a service list document."""
    if isinstance(document, list):
        for item in document:
            budget_service(item)
    return document


def budget_spec_object(document: Any) -> Any:
    """Bound a config or secret's labels and drop any `Spec.Data` before validation."""
    spec = document.get("Spec") if isinstance(document, dict) else None
    if isinstance(spec, dict):
        spec.pop("Data", None)
        spec["Labels"], spec[TRUNCATED_FIELD] = budget_labels(spec.get("Labels"))
    return document


def budget_spec_objects(document: Any) -> Any:
    """Apply `budget_spec_object` to each entry of a config or secret list document."""
    if isinstance(document, list):
        for item in document:
            budget_spec_object(item)
    return document
