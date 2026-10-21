from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Node:
    node_id: str
    name: str
    address: str
    system: str
    cpu_total: int
    cpu_usage: float
    ram_total: float
    ram_free: float
    disk_total: float
    disk_free: float
    docker: bool
    state: str = "free"
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Job:
    job_id: str
    kind: str
    cpu: int
    ram: float
    disk: float
    command: str = ""
    image: str = ""
    ports: str = ""
    node_id: str = ""
    node_name: str = ""
    state: str = "queued"
    output: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

