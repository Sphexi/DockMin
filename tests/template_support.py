"""Documents, fakes and helpers for the template catalogue tests."""

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from app.services.template_fetch import CatalogueFetchError, FetchedBody
from app.services.template_normalise import NormalisedCatalogue, normalise

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "templates"
LINUXSERVER_URL = "https://catalogue.example.test/api/v1/images"
PORTAINER_URL = "https://catalogue.example.test/templates.json"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_document(name: str) -> Any:
    return json.loads(fixture_text(name))


def normalised(name: str, kind: str) -> NormalisedCatalogue:
    return normalise(kind, fixture_document(name))


def digest_key(index: int, title: str) -> str:
    return f"i{index}-{hashlib.sha256(title.encode()).hexdigest()[:8]}"


def portainer_document(*templates: dict[str, Any], version: str = "3") -> bytes:
    return json.dumps({"version": version, "templates": list(templates)}).encode()


def container(title: str, **changes: Any) -> dict[str, Any]:
    return {"type": 1, "title": title, "image": "busybox", **changes}


def gzipped(body: bytes) -> bytes:
    return gzip.compress(body)


class ScriptedFetcher:
    """Answers each fetch from a queue and counts the calls; never touches the network."""

    def __init__(self, *outcomes: bytes | CatalogueFetchError) -> None:
        self.outcomes = list(outcomes)
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedBody:
        self.urls.append(url)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, CatalogueFetchError):
            raise outcome
        return FetchedBody(outcome, "")


def linuxserver_image(name: str, **config: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} application.",
        "category": "Tools",
        "architectures": [{"arch": "x86_64", "tag": "amd64-latest"}],
        "config": config,
    }


def linuxserver_document(*images: dict[str, Any]) -> bytes:
    document = {"status": "OK", "data": {"repositories": {"linuxserver": list(images)}}}
    return json.dumps(document).encode()
