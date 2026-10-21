import sys
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cluster.models import Node
from cluster.scheduler import select_node


def make_node(name, cpu=4, ram=8, disk=100, docker=True, state="free", age=0):
    return Node(
        node_id=name,
        name=name,
        address="127.0.0.1",
        system="Test OS",
        cpu_total=cpu,
        cpu_usage=10,
        ram_total=ram,
        ram_free=ram,
        disk_total=disk,
        disk_free=disk,
        docker=docker,
        state=state,
        last_seen=time.time() - age,
    )


class SchedulerTests(unittest.TestCase):
    def test_selects_smallest_suitable_node(self):
        small = make_node("small", ram=8)
        large = make_node("large", ram=32)
        self.assertEqual(select_node([large, small], 2, 4, 5, False), small)

    def test_rejects_node_without_enough_resources(self):
        node = make_node("weak", cpu=2, ram=4)
        self.assertIsNone(select_node([node], 4, 8, 5, False))

    def test_docker_job_requires_docker(self):
        plain = make_node("plain", docker=False)
        self.assertIsNone(select_node([plain], 1, 1, 1, True))

    def test_ignores_busy_node(self):
        busy = make_node("busy", state="busy")
        self.assertIsNone(select_node([busy], 1, 1, 1, False))

    def test_ignores_offline_node(self):
        offline = make_node("offline", age=30)
        self.assertIsNone(select_node([offline], 1, 1, 1, False))


if __name__ == "__main__":
    unittest.main()

