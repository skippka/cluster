import time
from collections.abc import Iterable

from .models import Node


def is_online(node: Node, timeout: int = 15) -> bool:
    return time.time() - node.last_seen < timeout


def select_node(
    nodes: Iterable[Node], cpu: int, ram: float, disk: float, docker: bool
) -> Node | None:
    candidates = []
    for node in nodes:
        if not is_online(node) or node.state != "free":
            continue
        if docker and not node.docker:
            continue
        if node.cpu_total < cpu or node.ram_free < ram or node.disk_free < disk:
            continue
        score = (node.ram_free - ram, node.cpu_total - cpu, node.disk_free - disk)
        candidates.append((score, node))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]

