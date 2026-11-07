"""Create forms, a recreatable container, and a recording pull-credential resolver."""

import copy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import SecretStr

from app.auth.principal import Principal
from app.db.engine import Database, Session
from app.db.repositories.clocked import Clock
from app.services.activity import ActivityRecorder
from app.services.container_create import ContainerCreateService
from app.services.container_recreate import ContainerRecreateService
from app.services.pull_credentials import CredentialChoice, PullCredentials
from tests.access_support import container_id
from tests.docker_engine.container_engine import Box, ContainerEngine
from tests.services.conftest import ContainerRig

IMAGE = "nginx:1"
OLD = container_id(0xB1)
NETWORK_NUMBER = 0xE1
NETWORK = "appnet"
VOLUME = "data"
AUTH = "c2VjcmV0LXJlZ2lzdHJ5LWF1dGg="
CREDENTIAL_SET_ID = 7
OLD_ENV = "FOO=old"
MIB = 1024 * 1024


def create_form(**fields: str) -> dict[str, str]:
    """A create submission for one image that the daemon holds; keyword fields override it."""
    return {"image": IMAGE, "pull": "missing", "start": "on", "network_mode": "bridge"} | fields


@dataclass(slots=True)
class ResolverStub:
    """Hands out one set of credentials and records every reference it was asked about."""

    credentials: PullCredentials = field(
        default_factory=lambda: PullCredentials(CREDENTIAL_SET_ID, SecretStr(AUTH))
    )
    asked: list[tuple[int, str]] = field(default_factory=list)
    principals: list[Principal | None] = field(default_factory=list)
    choices: list[CredentialChoice | None] = field(default_factory=list)

    async def resolve(
        self,
        environment_id: int,
        reference: str,
        *,
        principal: Principal | None,
        choice: CredentialChoice | None = None,
        min_remaining: timedelta | None = None,
    ) -> PullCredentials:
        self.asked.append((environment_id, reference))
        self.principals.append(principal)
        self.choices.append(choice)
        return self.credentials


def creation_with(
    rig: ContainerRig, clock: Clock, resolver: ResolverStub
) -> ContainerCreateService:
    """The rig's creation service, rebuilt around a different credential resolver."""
    db: Session = rig.db
    database: Database = rig.database
    return ContainerCreateService(
        db=db,
        database=database,
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        locks=rig.engine.locks,
        controls=rig.access.controls,
        activity=ActivityRecorder(db, clock),
        clock=clock,
        credentials=resolver,
    )


def recreation_with(
    rig: ContainerRig, clock: Clock, resolver: ResolverStub
) -> ContainerRecreateService:
    """The rig's recreation service, rebuilt around a different credential resolver."""
    db: Session = rig.db
    database: Database = rig.database
    return ContainerRecreateService(
        db=db,
        database=database,
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        locks=rig.engine.locks,
        controls=rig.access.controls,
        activity=ActivityRecorder(db, clock),
        clock=clock,
        credentials=resolver,
    )


def old_body(
    network_id: str,
    *,
    config: dict[str, Any] | None = None,
    host: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The create body of the container the recreate tests start from."""
    body: dict[str, Any] = {
        "Image": IMAGE,
        "Env": ["APP=1"],
        "Labels": {"tier": "front"},
        "Cmd": ["serve", "--fast"],
        "Volumes": {"/cache": {}},
        "HostConfig": {
            "Binds": [f"{VOLUME}:/data"],
            "RestartPolicy": {"Name": "always", "MaximumRetryCount": 0},
            "Memory": 256 * MIB,
            "NetworkMode": NETWORK,
            **copy.deepcopy(host or {}),
        },
        "NetworkingConfig": {"EndpointsConfig": {network_id: {"Aliases": ["web"]}}},
    }
    body.update(copy.deepcopy(config or {}))
    return body


def seed_recreatable(
    host: ContainerEngine,
    *,
    state: str = "running",
    name: str = "web",
    config: dict[str, Any] | None = None,
    host_config: dict[str, Any] | None = None,
) -> Box:
    """Add the image, network and volume a container needs, and the container itself."""
    host.add_image(IMAGE, config={"Env": ["PATH=/bin", OLD_ENV], "Cmd": ["nginx"]})
    network_id = host.add_network(NETWORK, NETWORK_NUMBER)
    host.add_volume(VOLUME)
    return host.add_configured(
        OLD, name, old_body(network_id, config=config, host=host_config), state
    )


def anonymous_volume_of(box: Box) -> str:
    """The generated name of the anonymous volume mounted at /cache."""
    generated: str = next(m["Name"] for m in box.mounts if m["Destination"] == "/cache")
    return generated
