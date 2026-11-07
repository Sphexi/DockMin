"""Shared constants and builders for the Swarm tests."""

import json
import re
from html import unescape
from pathlib import Path
from typing import Any

from httpx import Response

SAMPLES = Path(__file__).parent / "docker_engine" / "swarm_samples"
SENTINEL = "TOPSECRET-sentinel-7f3a91c2e5"
TOKEN = "c2VjcmV0LXRva2Vu"
EMPTY_AUTH = "e30="
NAMESPACE_LABEL = "com.docker.stack.namespace"
COMPOSE_LABEL = "com.docker.compose.project"
NODE = "abcdefghijklmnopqrstuvwxy"
MISSING_ID = "z" * 25


def sample_bytes(name: str) -> bytes:
    """Return the raw bytes of a response recorded from a real Engine."""
    return (SAMPLES / name).read_bytes()


def sample_json(name: str) -> Any:
    """Return a response recorded from a real Engine, decoded."""
    return json.loads(sample_bytes(name))


def sample_specs() -> list[tuple[str, str, dict[str, Any]]]:
    """Return (sample, key, spec) for every service Spec and PreviousSpec among the samples."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for name in (
        "05-GET-services-list-status.bin",
        "10-GET-A-after-rollback.bin",
        "18-GET-services-label-key-filter.bin",
    ):
        document = sample_json(name)
        for item in document if isinstance(document, list) else [document]:
            for key in ("Spec", "PreviousSpec"):
                if item.get(key):
                    found.append((name, key, item[key]))
    return found


def hidden_fields(response: Response) -> dict[str, str]:
    """Return every hidden input of the page as a name to value mapping."""
    pattern = r'<input type="hidden" name="([^"]*)" value="([^"]*)"'
    return {unescape(n): unescape(v) for n, v in re.findall(pattern, response.text)}


def field_values(response: Response) -> dict[str, str]:
    """Return the text and number inputs of a service form as a name to value mapping."""
    pattern = (
        r'<input class="field__input[^"]*" id="[^"]*" name="([^"]*)" '
        r'type="(?:text|number)"[^>]*? value="([^"]*)"'
    )
    return {unescape(n): unescape(v) for n, v in re.findall(pattern, response.text)}


def form_action(response: Response) -> str:
    """Return the action of the first form that posts a confirmation."""
    found = re.search(
        r'<form class="form create-form" method="post" action="([^"]*)"', response.text
    )
    assert found is not None
    return unescape(found.group(1))


def without_token(fields: dict[str, str]) -> dict[str, str]:
    """Return form fields without the CSRF token, for a client that adds its own."""
    return {k: v for k, v in fields.items() if k != "csrf_token"}
