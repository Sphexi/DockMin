"""Pydantic models for the Engine responses this product reads; unknown fields are ignored."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_INT64_MAX = 2**63 - 1
_MAX_PROGRESS_BYTES = 2**53
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class _EngineModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _null_means_absent(cls, data: Any) -> Any:
        # The Engine serialises empty maps and lists as null; dropping them lets defaults apply.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data


class SwarmState(StrEnum):
    """The daemon's Swarm membership, as reported by the `Swarm` ping header."""

    INACTIVE = "inactive"
    PENDING = "pending"
    ERROR = "error"
    LOCKED = "locked"
    WORKER = "worker"
    MANAGER = "manager"
    UNKNOWN = "unknown"

    @classmethod
    def from_header(cls, value: str | None) -> "SwarmState":
        """Map a header value such as "active/manager" to a state."""
        if value is None:
            return cls.UNKNOWN
        leaf = value.strip().lower().removeprefix("active/")
        try:
            return cls(leaf)
        except ValueError:
            return cls.UNKNOWN


class DaemonInfo(BaseModel):
    """What negotiation learned about a daemon."""

    model_config = ConfigDict(frozen=True)

    engine_version: str
    daemon_api_version: str
    min_api_version: str | None
    negotiated_api_version: str
    swarm_state: SwarmState


class PortBinding(_EngineModel):
    """A published or exposed container port."""

    ip: str | None = Field(default=None, alias="IP")
    private_port: int = Field(alias="PrivatePort")
    public_port: int | None = Field(default=None, alias="PublicPort")
    type: str = Field(default="tcp", alias="Type")


class HostBinding(_EngineModel):
    """One host address a container port is published on."""

    host_ip: str = Field(default="", alias="HostIp")
    host_port: str = Field(default="", alias="HostPort")


class SummaryMount(_EngineModel):
    """The identifying part of a mount in a container list row; the host path is not kept."""

    type: str = Field(default="", alias="Type")
    name: str | None = Field(default=None, alias="Name")
    destination: str = Field(default="", alias="Destination")


class SummaryEndpoint(_EngineModel):
    """A container's endpoint on a network as the list reports it; the id is empty until start."""

    name: str = Field(default="", alias="Name")
    network_id: str = Field(default="", alias="NetworkID")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _summary_endpoints(settings: Any) -> list[dict[str, str]]:
    networks = settings.get("Networks") if isinstance(settings, dict) else None
    if not isinstance(networks, dict):
        return []
    return [
        {"Name": name, "NetworkID": _text(endpoint.get("NetworkID"))}
        for name, endpoint in networks.items()
        if isinstance(name, str) and isinstance(endpoint, dict)
    ]


class ContainerSummary(_EngineModel):
    """A row of the container list."""

    id: str = Field(alias="Id")
    names: list[str] = Field(default_factory=list, alias="Names")
    image: str = Field(alias="Image")
    image_id: str = Field(default="", alias="ImageID")
    created: int = Field(alias="Created")
    state: str = Field(alias="State")
    status: str = Field(default="", alias="Status")
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")
    labels_truncated: bool = Field(default=False, alias="LabelsTruncated")
    ports: list[PortBinding] = Field(default_factory=list, alias="Ports")
    mounts: list[SummaryMount] = Field(default_factory=list, alias="Mounts")
    network_ids: list[str] = Field(default_factory=list, alias="NetworkIds")
    endpoints: list[SummaryEndpoint] = Field(default_factory=list, alias="Endpoints")
    network_mode: str = Field(default="", alias="NetworkMode")

    @model_validator(mode="before")
    @classmethod
    def _from_nested_blocks(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "NetworkSettings" in data:
            endpoints = _summary_endpoints(data["NetworkSettings"])
            data["Endpoints"] = endpoints
            data["NetworkIds"] = list(
                dict.fromkeys(e["NetworkID"] for e in endpoints if e["NetworkID"])
            )
        if "HostConfig" in data:
            host_config = data["HostConfig"]
            mode = host_config.get("NetworkMode") if isinstance(host_config, dict) else None
            data["NetworkMode"] = _text(mode)
        return data


HEALTH_LOG_ENTRIES = 5
HEALTH_OUTPUT_CHARS = 4096


class HealthLogEntry(_EngineModel):
    """One run of a container's health check command."""

    start: str = Field(default="", alias="Start")
    end: str = Field(default="", alias="End")
    exit_code: int = Field(default=0, alias="ExitCode")
    output: str = Field(default="", alias="Output", repr=False)

    @field_validator("output", mode="after")
    @classmethod
    def _bounded_output(cls, value: str) -> str:
        return value[:HEALTH_OUTPUT_CHARS]


class HealthState(_EngineModel):
    """A container health check's current status and its most recent runs, oldest first."""

    status: str = Field(alias="Status")
    failing_streak: int = Field(default=0, alias="FailingStreak")
    log: list[HealthLogEntry] = Field(default_factory=list, alias="Log")

    @field_validator("log", mode="before")
    @classmethod
    def _recent_entries(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict)][-HEALTH_LOG_ENTRIES:]


class ContainerState(_EngineModel):
    """The runtime state block of a container inspect."""

    status: str = Field(alias="Status")
    running: bool = Field(alias="Running")
    paused: bool = Field(default=False, alias="Paused")
    restarting: bool = Field(default=False, alias="Restarting")
    oom_killed: bool = Field(default=False, alias="OOMKilled")
    dead: bool = Field(default=False, alias="Dead")
    pid: int = Field(default=0, alias="Pid")
    exit_code: int = Field(default=0, alias="ExitCode")
    error: str = Field(default="", alias="Error")
    started_at: str = Field(default="", alias="StartedAt")
    finished_at: str = Field(default="", alias="FinishedAt")
    health: HealthState | None = Field(default=None, alias="Health")


class ContainerConfig(_EngineModel):
    """The creation-time configuration of a container."""

    hostname: str = Field(default="", alias="Hostname")
    image: str = Field(default="", alias="Image")
    tty: bool = Field(default=False, alias="Tty")
    open_stdin: bool = Field(default=False, alias="OpenStdin")
    stdin_once: bool = Field(default=False, alias="StdinOnce")
    cmd: list[str] = Field(default_factory=list, alias="Cmd")
    entrypoint: list[str] = Field(default_factory=list, alias="Entrypoint")
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")
    stop_timeout: int | None = Field(default=None, alias="StopTimeout")
    # Environment variables routinely hold secrets, so they stay out of repr and log lines.
    env: list[str] = Field(default_factory=list, alias="Env", repr=False)


class MountPoint(_EngineModel):
    """A mount attached to a container."""

    type: str = Field(default="", alias="Type")
    name: str | None = Field(default=None, alias="Name")
    source: str = Field(default="", alias="Source")
    destination: str = Field(default="", alias="Destination")
    read_write: bool = Field(default=True, alias="RW")


class EndpointSettings(_EngineModel):
    """A container's attachment to one network."""

    network_id: str = Field(default="", alias="NetworkID")
    ip_address: str = Field(default="", alias="IPAddress")
    gateway: str = Field(default="", alias="Gateway")
    mac_address: str = Field(default="", alias="MacAddress")
    global_ipv6_address: str = Field(default="", alias="GlobalIPv6Address")
    aliases: list[str] = Field(default_factory=list, alias="Aliases")
    ipam_config: dict[str, Any] | None = Field(default=None, alias="IPAMConfig")
    links: list[str] = Field(default_factory=list, alias="Links")
    driver_opts: dict[str, str] = Field(default_factory=dict, alias="DriverOpts")
    gw_priority: int | None = Field(default=None, alias="GwPriority")


class NetworkSettings(_EngineModel):
    """A container's network attachments."""

    networks: dict[str, EndpointSettings] = Field(default_factory=dict, alias="Networks")
    ports: dict[str, list[HostBinding] | None] | None = Field(default=None, alias="Ports")


class ContainerInspect(_EngineModel):
    """The detail view of one container."""

    id: str = Field(alias="Id")
    name: str = Field(alias="Name")
    created: str = Field(alias="Created")
    image: str = Field(alias="Image")
    restart_count: int = Field(default=0, alias="RestartCount")
    state: ContainerState = Field(alias="State")
    config: ContainerConfig = Field(alias="Config")
    # The same `Config` object unfiltered; environment variables stay out of repr and log lines.
    config_document: dict[str, Any] = Field(default_factory=dict, alias="Config", repr=False)
    host_config: dict[str, Any] = Field(default_factory=dict, alias="HostConfig", repr=False)
    network_settings: NetworkSettings = Field(
        default_factory=NetworkSettings, alias="NetworkSettings"
    )
    mounts: list[MountPoint] = Field(default_factory=list, alias="Mounts")
    labels_truncated: bool = Field(default=False, alias="LabelsTruncated")


class MemoryStats(_EngineModel):
    """Memory use within one stats frame."""

    usage: int = Field(default=0, alias="usage")
    limit: int = Field(default=0, alias="limit")
    stats: dict[str, int] = Field(default_factory=dict, alias="stats")


class CpuUsage(_EngineModel):
    """Cumulative CPU time within one stats frame."""

    total_usage: int = Field(default=0, alias="total_usage")


class CpuStats(_EngineModel):
    """CPU counters within one stats frame."""

    cpu_usage: CpuUsage = Field(default_factory=CpuUsage, alias="cpu_usage")
    system_cpu_usage: int = Field(default=0, alias="system_cpu_usage")
    online_cpus: int = Field(default=0, alias="online_cpus")


class NetworkIo(_EngineModel):
    """Cumulative traffic on one interface."""

    rx_bytes: int = Field(default=0, alias="rx_bytes")
    tx_bytes: int = Field(default=0, alias="tx_bytes")


class BlockIoEntry(_EngineModel):
    """One block-device counter."""

    op: str = Field(default="", alias="op")
    value: int = Field(default=0, alias="value")


class BlockIoStats(_EngineModel):
    """Block I/O counters within one stats frame."""

    io_service_bytes_recursive: list[BlockIoEntry] = Field(
        default_factory=list, alias="io_service_bytes_recursive"
    )


class PidsStats(_EngineModel):
    """Process count within one stats frame."""

    current: int = Field(default=0, alias="current")


class ContainerStats(_EngineModel):
    """One frame of a container stats stream."""

    read: str = Field(default="", alias="read")
    cpu_stats: CpuStats = Field(default_factory=CpuStats, alias="cpu_stats")
    precpu_stats: CpuStats = Field(default_factory=CpuStats, alias="precpu_stats")
    memory_stats: MemoryStats = Field(default_factory=MemoryStats, alias="memory_stats")
    networks: dict[str, NetworkIo] = Field(default_factory=dict, alias="networks")
    blkio_stats: BlockIoStats = Field(default_factory=BlockIoStats, alias="blkio_stats")
    pids_stats: PidsStats = Field(default_factory=PidsStats, alias="pids_stats")


class ExecInspect(_EngineModel):
    """The state of an exec instance; the exit code is absent while it runs."""

    running: bool = Field(default=False, alias="Running")
    exit_code: int | None = Field(default=None, alias="ExitCode")


class ImageSummary(_EngineModel):
    """A row of the image list."""

    id: str = Field(alias="Id")
    repo_tags: list[str] = Field(default_factory=list, alias="RepoTags")
    repo_digests: list[str] = Field(default_factory=list, alias="RepoDigests")
    created: int = Field(alias="Created")
    size: int = Field(default=0, alias="Size")
    containers: int = Field(default=-1, alias="Containers")
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")


class ImageInspect(_EngineModel):
    """The detail view of one image."""

    id: str = Field(alias="Id")
    repo_tags: list[str] = Field(default_factory=list, alias="RepoTags")
    repo_digests: list[str] = Field(default_factory=list, alias="RepoDigests")
    created: str = Field(default="", alias="Created")
    size: int = Field(default=0, alias="Size")
    architecture: str = Field(default="", alias="Architecture")
    os: str = Field(default="", alias="Os")
    config: dict[str, Any] = Field(default_factory=dict, alias="Config", repr=False)


class ProgressDetail(_EngineModel):
    """Byte counts of one layer; values the Engine sends out of range are clamped."""

    current: int = Field(default=0, alias="current")
    total: int = Field(default=0, alias="total")

    @field_validator("current", "total", mode="before")
    @classmethod
    def _clamped(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return min(max(value, 0), _MAX_PROGRESS_BYTES)


class PullProgress(_EngineModel):
    """One progress message of an image pull; the human progress bar text is never displayed."""

    status: str = Field(default="", alias="status")
    id: str | None = Field(default=None, alias="id")
    progress: str | None = Field(default=None, alias="progress")
    progress_detail: ProgressDetail = Field(default_factory=ProgressDetail, alias="progressDetail")


class PushAux(_EngineModel):
    """The summary object the Engine sends once a push has been accepted."""

    tag: str | None = Field(default=None, alias="Tag")
    digest: str | None = Field(default=None, alias="Digest")
    size: int | None = Field(default=None, alias="Size")

    @field_validator("digest", mode="before")
    @classmethod
    def _valid_digest(cls, value: Any) -> str | None:
        if isinstance(value, str) and _SHA256_DIGEST.fullmatch(value):
            return value
        return None


class PushProgress(_EngineModel):
    """One progress message of an image push; `aux` appears once, after the last layer."""

    status: str = Field(default="", alias="status")
    id: str | None = Field(default=None, alias="id")
    progress: str | None = Field(default=None, alias="progress")
    progress_detail: ProgressDetail = Field(default_factory=ProgressDetail, alias="progressDetail")
    aux: PushAux | None = Field(default=None, alias="aux")


class ImageDeleteItem(_EngineModel):
    """One entry of the list an image removal returns: a reference untagged or an image deleted."""

    untagged: str | None = Field(default=None, alias="Untagged")
    deleted: str | None = Field(default=None, alias="Deleted")


class ImageHistoryEntry(_EngineModel):
    """One layer of an image's history; `created_by` is the build command and may hold secrets."""

    id: str = Field(default="", alias="Id")
    created: int = Field(default=0, alias="Created")
    created_by: str = Field(default="", alias="CreatedBy", repr=False)
    tags: list[str] = Field(default_factory=list, alias="Tags")
    size: int = Field(default=0, alias="Size")


class NetworkContainer(_EngineModel):
    """A container attached to a network."""

    name: str = Field(default="", alias="Name")
    ipv4_address: str = Field(default="", alias="IPv4Address")
    ipv6_address: str = Field(default="", alias="IPv6Address")
    mac_address: str = Field(default="", alias="MacAddress")


class IpamPool(_EngineModel):
    """One address pool of a network; the values are the Engine's text, not validated addresses."""

    subnet: str = Field(default="", alias="Subnet")
    gateway: str = Field(default="", alias="Gateway")
    ip_range: str = Field(default="", alias="IPRange")

    @field_validator("subnet", "gateway", "ip_range", mode="before")
    @classmethod
    def _text_only(cls, value: Any) -> str:
        return _text(value)


class Network(_EngineModel):
    """A network, as listed or inspected."""

    id: str = Field(alias="Id")
    name: str = Field(alias="Name")
    driver: str = Field(default="", alias="Driver")
    scope: str = Field(default="", alias="Scope")
    internal: bool = Field(default=False, alias="Internal")
    attachable: bool = Field(default=False, alias="Attachable")
    ingress: bool = Field(default=False, alias="Ingress")
    enable_ipv6: bool = Field(default=False, alias="EnableIPv6")
    created: str = Field(default="", alias="Created")
    ipam: list[IpamPool] = Field(default_factory=list, alias="IPAM")
    ipam_driver: str = ""
    # Driver options name host interfaces and can carry credentials.
    options: dict[str, str] = Field(default_factory=dict, alias="Options", repr=False)
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")
    labels_truncated: bool = Field(default=False, alias="LabelsTruncated")
    containers: dict[str, NetworkContainer] = Field(default_factory=dict, alias="Containers")

    @model_validator(mode="before")
    @classmethod
    def _ipam_driver_of_the_ipam_block(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("IPAM"), dict):
            driver = value["IPAM"].get("Driver")
            if isinstance(driver, str):
                return {**value, "ipam_driver": driver[:64]}
        return value

    @field_validator("ipam", mode="before")
    @classmethod
    def _pools_of_the_ipam_block(cls, value: Any) -> Any:
        # The Engine nests the pools under `IPAM.Config`; entries that are not objects are dropped.
        pools = value.get("Config") if isinstance(value, dict) else value
        if not isinstance(pools, list):
            return []
        return [pool for pool in pools if isinstance(pool, dict | IpamPool)]


class Volume(_EngineModel):
    """A volume."""

    name: str = Field(alias="Name")
    driver: str = Field(default="", alias="Driver")
    # The mountpoint is a host path and the options can carry credentials.
    mountpoint: str = Field(default="", alias="Mountpoint", repr=False)
    scope: str = Field(default="", alias="Scope")
    created_at: str = Field(default="", alias="CreatedAt")
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")
    labels_truncated: bool = Field(default=False, alias="LabelsTruncated")
    options: dict[str, str] = Field(default_factory=dict, alias="Options", repr=False)


class SwarmInfo(_EngineModel):
    """The Swarm block of the system info; the cluster's TLS and join material is never read."""

    node_id: str = Field(default="", alias="NodeID")
    node_addr: str = Field(default="", alias="NodeAddr")
    local_node_state: str = Field(default="", alias="LocalNodeState")
    control_available: bool = Field(default=False, alias="ControlAvailable")
    cluster_id: str = Field(default="", alias="ClusterID")
    managers: int | None = Field(default=None, alias="Managers")
    nodes: int | None = Field(default=None, alias="Nodes")

    @model_validator(mode="before")
    @classmethod
    def _cluster_identity(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cluster = data.get("Cluster")
        return {**data, "ClusterID": _text(cluster.get("ID")) if isinstance(cluster, dict) else ""}

    @field_validator("managers", "nodes", mode="before")
    @classmethod
    def _plausible_count(cls, value: Any) -> int | None:
        # A count the Engine mangles is unknown; it must not fail the whole `/info` read.
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _INT64_MAX:
            return None
        return value


class SystemInfo(_EngineModel):
    """Host-level facts from `GET /info`."""

    id: str = Field(default="", alias="ID")
    name: str = Field(default="", alias="Name")
    server_version: str = Field(default="", alias="ServerVersion")
    default_runtime: str = Field(default="", alias="DefaultRuntime")
    logging_driver: str = Field(default="", alias="LoggingDriver")
    cgroup_version: str = Field(default="", alias="CgroupVersion")
    operating_system: str = Field(default="", alias="OperatingSystem")
    os_type: str = Field(default="", alias="OSType")
    architecture: str = Field(default="", alias="Architecture")
    ncpu: int = Field(default=0, alias="NCPU")
    mem_total: int = Field(default=0, alias="MemTotal")
    containers: int = Field(default=0, alias="Containers")
    containers_running: int = Field(default=0, alias="ContainersRunning")
    containers_paused: int = Field(default=0, alias="ContainersPaused")
    containers_stopped: int = Field(default=0, alias="ContainersStopped")
    images: int = Field(default=0, alias="Images")
    swarm: SwarmInfo = Field(default_factory=SwarmInfo, alias="Swarm")


class ImageDiskUsage(_EngineModel):
    """The image figure of `GET /system/df`: bytes of every layer, each counted once."""

    layers_size: int = Field(alias="LayersSize", ge=0)


class EventActor(_EngineModel):
    """The object an event is about."""

    id: str = Field(default="", alias="ID")
    attributes: dict[str, str] = Field(default_factory=dict, alias="Attributes")


class EngineEvent(_EngineModel):
    """One message from the events stream."""

    type: str = Field(alias="Type")
    action: str = Field(alias="Action")
    actor: EventActor = Field(default_factory=EventActor, alias="Actor")
    scope: str = Field(default="local", alias="scope")
    time: int = Field(default=0, alias="time")
    time_nano: int = Field(default=0, alias="timeNano")

    @field_validator("time", "time_nano")
    @classmethod
    def _out_of_range_is_absent(cls, value: int) -> int:
        # Rejecting would raise out of the watcher and end it; an unusable stamp is just unknown.
        return value if 0 <= value <= _INT64_MAX else 0


class SwarmCluster(_EngineModel):
    """Swarm identity; the join tokens in the response are deliberately not modelled."""

    id: str = Field(alias="ID")
    created_at: str = Field(default="", alias="CreatedAt")
    updated_at: str = Field(default="", alias="UpdatedAt")


class ObjectVersion(_EngineModel):
    """The optimistic-concurrency counter every Swarm object carries."""

    index: Annotated[int, Field(strict=True, ge=0, le=_INT64_MAX)] = Field(alias="Index")


def _string_pairs(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _unsigned(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _INT64_MAX:
        return 0
    return value


class NodePlatform(_EngineModel):
    """A node's platform."""

    architecture: str = Field(default="", alias="Architecture")
    os: str = Field(default="", alias="OS")


class NodeDescription(_EngineModel):
    """A node's self-description."""

    hostname: str = Field(default="", alias="Hostname")
    platform: NodePlatform = Field(default_factory=NodePlatform, alias="Platform")
    engine_version: str = Field(default="", alias="EngineVersion")
    nano_cpus: int = Field(default=0, alias="NanoCPUs")
    memory_bytes: int = Field(default=0, alias="MemoryBytes")
    engine_labels: dict[str, str] = Field(default_factory=dict, alias="EngineLabels")

    @model_validator(mode="before")
    @classmethod
    def _from_nested_blocks(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        resources = data.get("Resources")
        engine = data.get("Engine")
        resources = resources if isinstance(resources, dict) else {}
        engine = engine if isinstance(engine, dict) else {}
        return {
            **data,
            "NanoCPUs": _unsigned(resources.get("NanoCPUs")),
            "MemoryBytes": _unsigned(resources.get("MemoryBytes")),
            "EngineVersion": _text(engine.get("EngineVersion")),
            "EngineLabels": _string_pairs(engine.get("Labels")),
        }


class NodeSpec(_EngineModel):
    """A node's desired role and availability."""

    name: str = Field(default="", alias="Name")
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")
    role: str = Field(default="", alias="Role")
    availability: str = Field(default="", alias="Availability")


class NodeStatus(_EngineModel):
    """A node's observed state."""

    state: str = Field(default="", alias="State")
    addr: str = Field(default="", alias="Addr")


class ManagerStatus(_EngineModel):
    """A manager node's consensus state."""

    leader: bool = Field(default=False, alias="Leader")
    reachability: str = Field(default="", alias="Reachability")
    addr: str = Field(default="", alias="Addr")


class SwarmNode(_EngineModel):
    """A Swarm node."""

    id: str = Field(alias="ID")
    version: ObjectVersion = Field(alias="Version")
    description: NodeDescription = Field(default_factory=NodeDescription, alias="Description")
    spec: NodeSpec = Field(default_factory=NodeSpec, alias="Spec")
    status: NodeStatus = Field(default_factory=NodeStatus, alias="Status")
    manager_status: ManagerStatus | None = Field(default=None, alias="ManagerStatus")


@dataclass(frozen=True, slots=True)
class NodeDocument:
    """A node as the Engine returned it, for an update that must send back every field."""

    id: str
    version: int
    spec: Mapping[str, Any] = field(repr=False)


class ServiceSpec(_EngineModel):
    """The parts of a service specification the product displays."""

    name: str = Field(default="", alias="Name")
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")
    labels_truncated: bool = Field(default=False, alias="LabelsTruncated")
    mode: dict[str, Any] = Field(default_factory=dict, alias="Mode")
    # The task template embeds container environment variables, so it stays out of repr.
    task_template: dict[str, Any] = Field(default_factory=dict, alias="TaskTemplate", repr=False)
    update_config: dict[str, Any] = Field(default_factory=dict, alias="UpdateConfig")
    rollback_config: dict[str, Any] = Field(default_factory=dict, alias="RollbackConfig")
    endpoint_spec: dict[str, Any] = Field(default_factory=dict, alias="EndpointSpec")


class EndpointPort(_EngineModel):
    """A port a service publishes."""

    protocol: str = Field(default="tcp", alias="Protocol")
    target_port: int = Field(default=0, alias="TargetPort")
    published_port: int | None = Field(default=None, alias="PublishedPort")
    publish_mode: str = Field(default="ingress", alias="PublishMode")


class VirtualIp(_EngineModel):
    """A service's virtual address on one network."""

    network_id: str = Field(default="", alias="NetworkID")
    addr: str = Field(default="", alias="Addr")


class ServiceEndpoint(_EngineModel):
    """A service's resolved ports and virtual addresses."""

    ports: list[EndpointPort] = Field(default_factory=list, alias="Ports")
    virtual_ips: list[VirtualIp] = Field(default_factory=list, alias="VirtualIPs")


class ServiceStatus(_EngineModel):
    """Task counts for a service."""

    running_tasks: int = Field(default=0, alias="RunningTasks")
    desired_tasks: int = Field(default=0, alias="DesiredTasks")
    completed_tasks: int = Field(default=0, alias="CompletedTasks")


class UpdateStatus(_EngineModel):
    """A service's rolling update; the state is the Engine's own text."""

    state: str = Field(default="", alias="State")
    message: str = Field(default="", alias="Message")
    started_at: str = Field(default="", alias="StartedAt")
    completed_at: str = Field(default="", alias="CompletedAt")


class SwarmService(_EngineModel):
    """A Swarm service."""

    id: str = Field(alias="ID")
    version: ObjectVersion = Field(alias="Version")
    created_at: str = Field(default="", alias="CreatedAt")
    updated_at: str = Field(default="", alias="UpdatedAt")
    spec: ServiceSpec = Field(default_factory=ServiceSpec, alias="Spec")
    previous_spec: ServiceSpec | None = Field(default=None, alias="PreviousSpec")
    endpoint: ServiceEndpoint = Field(default_factory=ServiceEndpoint, alias="Endpoint")
    service_status: ServiceStatus | None = Field(default=None, alias="ServiceStatus")
    update_status: UpdateStatus | None = Field(default=None, alias="UpdateStatus")


@dataclass(frozen=True, slots=True)
class ServiceDocument:
    """A service as the Engine returned it, unbudgeted, for an update to send back whole."""

    id: str
    version: int
    spec: Mapping[str, Any] = field(repr=False)
    previous_spec: Mapping[str, Any] | None = field(default=None, repr=False)


class ContainerStatus(_EngineModel):
    """The container a task runs; the exit code means something only once the task has ended."""

    container_id: str = Field(default="", alias="ContainerID")
    exit_code: int | None = Field(default=None, alias="ExitCode")


class TaskStatus(_EngineModel):
    """A task's observed state; `err` and `message` are the Engine's text."""

    state: str = Field(default="", alias="State")
    message: str = Field(default="", alias="Message")
    err: str = Field(default="", alias="Err")
    timestamp: str = Field(default="", alias="Timestamp")
    container_status: ContainerStatus | None = Field(default=None, alias="ContainerStatus")


class SwarmTask(_EngineModel):
    """A Swarm task; its spec, which embeds container environment variables, is not read."""

    id: str = Field(alias="ID")
    version: ObjectVersion = Field(alias="Version")
    service_id: str = Field(default="", alias="ServiceID")
    node_id: str = Field(default="", alias="NodeID")
    slot: int | None = Field(default=None, alias="Slot")
    desired_state: str = Field(default="", alias="DesiredState")
    status: TaskStatus = Field(default_factory=TaskStatus, alias="Status")
    created_at: str = Field(default="", alias="CreatedAt")
    updated_at: str = Field(default="", alias="UpdatedAt")


def _flatten_object_spec(data: Any, flag: str, source: str) -> Any:
    """Lift a config or secret's `Spec` fields beside its id; `flag` records if `source` is set."""
    if not isinstance(data, dict):
        return data
    spec = data.get("Spec")
    if not isinstance(spec, dict):
        return data
    return {
        **data,
        "Name": spec.get("Name"),
        "Labels": spec.get("Labels"),
        "LabelsTruncated": spec.get("LabelsTruncated") is True,
        flag: bool(spec.get(source)),
    }


class SwarmConfig(_EngineModel):
    """A config's metadata; its content is read only through `ConfigsApi.inspect`."""

    id: str = Field(alias="ID")
    version: ObjectVersion = Field(alias="Version")
    created_at: str = Field(default="", alias="CreatedAt")
    updated_at: str = Field(default="", alias="UpdatedAt")
    name: str = Field(alias="Name")
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")
    labels_truncated: bool = Field(default=False, alias="LabelsTruncated")
    templated: bool = Field(default=False, alias="Templated")

    @model_validator(mode="before")
    @classmethod
    def _from_spec(cls, data: Any) -> Any:
        return _flatten_object_spec(data, "Templated", "Templating")


@dataclass(frozen=True, slots=True)
class ConfigDocument:
    """A config with its decoded content."""

    config: SwarmConfig
    data: bytes = field(repr=False)


class SwarmSecret(_EngineModel):
    """A secret's metadata. There is no field for its value, which the Engine never returns."""

    id: str = Field(alias="ID")
    version: ObjectVersion = Field(alias="Version")
    created_at: str = Field(default="", alias="CreatedAt")
    updated_at: str = Field(default="", alias="UpdatedAt")
    name: str = Field(alias="Name")
    labels: dict[str, str] = Field(default_factory=dict, alias="Labels")
    labels_truncated: bool = Field(default=False, alias="LabelsTruncated")
    has_driver: bool = Field(default=False, alias="HasDriver")

    @model_validator(mode="before")
    @classmethod
    def _from_spec(cls, data: Any) -> Any:
        return _flatten_object_spec(data, "HasDriver", "Driver")


class DistributionDescriptor(_EngineModel):
    """What a registry reports for a reference: the content digest it resolves to."""

    digest: str = Field(alias="Digest")

    @model_validator(mode="before")
    @classmethod
    def _from_descriptor(cls, data: Any) -> Any:
        descriptor = data.get("Descriptor") if isinstance(data, dict) else None
        return {"Digest": descriptor.get("digest")} if isinstance(descriptor, dict) else data

    @field_validator("digest")
    @classmethod
    def _sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST.fullmatch(value) is None:
            raise ValueError("digest is not a sha256 digest")
        return value
