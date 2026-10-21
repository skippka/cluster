import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from cluster.corpus import analyze
from cluster.controller import ClusterController, show_dashboard
from cluster.protocol import api_request
from cluster.sharing import DEFAULT_POLICY, evaluate, load_policy
from cluster.swarm import Swarm, LEASE_SECONDS, MAX_ATTEMPTS
from cluster.worker import run_worker


class PolicyTests(unittest.TestCase):
    def offer(self, **changes):
        metrics = {"cpu_total": 8, "cpu_usage": 20, "ram_free": 6}
        power = {"ac": False, "battery": 80}
        return evaluate({**DEFAULT_POLICY, **changes}, metrics, power)

    def test_owner_reserves_and_shared_budgets(self):
        offer = self.offer(reserve_ram_gb=5, max_cores=1)
        self.assertTrue(offer["ready"])
        self.assertEqual((offer["ram"], offer["cores"]), (1, 1))

    def test_pause_low_battery_ac_and_busy(self):
        self.assertFalse(self.offer(enabled=False)["ready"])
        self.assertFalse(self.offer(ac_only=True)["ready"])
        self.assertFalse(self.offer(minimum_battery=90)["ready"])
        self.assertFalse(self.offer(max_host_cpu=10)["ready"])

    def test_unknown_ac_is_not_reported_as_plugged_in(self):
        offer = evaluate({**DEFAULT_POLICY, "ac_only": True},
                         {"cpu_total": 4, "cpu_usage": 0, "ram_free": 10},
                         {"battery": None, "ac": None})
        self.assertFalse(offer["ready"])

    def test_invalid_policy_pauses_instead_of_ignoring_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sharing.json"
            for text in ('{}broken', '{"enabled":"false"}', '{"max_ram_gb":NaN}'):
                path.write_text(text)
                self.assertFalse(load_policy(directory)["enabled"])


class SwarmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "input"
        self.folder.mkdir()
        (self.folder / "a.txt").write_text("hello world\nhello", encoding="utf-8")
        self.now = 1000
        self.swarm = Swarm(self.root / "data", clock=lambda: self.now)

    def tearDown(self):
        self.swarm.close()
        self.temp.cleanup()

    def result(self, job, node="one"):
        return {"job_id": job["job_id"], "token": job["token"], "node_id": node,
                "state": "done", "result": analyze(job["payload"])}

    def test_expired_attempt_is_reassigned_and_late_result_is_fenced(self):
        batch, _, _ = self.swarm.submit(self.folder)
        first = self.swarm.claim("one", "Laptop One")
        self.now += LEASE_SECONDS + 1
        second = self.swarm.claim("two", "Laptop Two")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first["token"], second["token"])
        self.assertFalse(self.swarm.complete(self.result(first)))
        self.assertTrue(self.swarm.complete(self.result(second, "two")))
        self.assertTrue(self.swarm.complete(self.result(second, "two")))
        target, report = self.swarm.export(batch)
        self.assertEqual(report["files"], 1)
        self.assertTrue(report["complete"])
        self.assertTrue(target.with_suffix(".csv").exists())

    def test_renewal_preserves_lease_and_wrong_worker_cannot_renew(self):
        self.swarm.submit(self.folder)
        job = self.swarm.claim("one", "one")
        self.now += 15
        self.assertFalse(self.swarm.renew("two", job["job_id"], job["token"]))
        self.assertTrue(self.swarm.renew("one", job["job_id"], job["token"]))
        self.now += 10
        self.assertIsNone(self.swarm.claim("two", "two"))
        self.assertTrue(self.swarm.complete(self.result(job)))

    def test_restart_keeps_accepted_chunks_and_requeues_only_unfinished(self):
        (self.folder / "b.txt").write_text("another", encoding="utf-8")
        batch, _, _ = self.swarm.submit(self.folder)
        done = self.swarm.claim("one", "one")
        self.swarm.complete(self.result(done))
        lost = self.swarm.claim("two", "two")
        self.swarm.close()
        self.swarm = Swarm(self.root / "data", clock=lambda: self.now)
        resumed = self.swarm.claim("three", "three")
        self.assertEqual(resumed["job_id"], lost["job_id"])
        self.assertFalse(self.swarm.complete(self.result(lost, "two")))
        self.swarm.complete(self.result(resumed, "three"))
        _, report = self.swarm.export(batch)
        self.assertTrue(report["complete"])
        self.assertEqual(report["files"], 2)

    def test_cancellation_revokes_running_attempt(self):
        batch, _, _ = self.swarm.submit(self.folder)
        job = self.swarm.claim("one", "one")
        self.assertTrue(self.swarm.cancel(batch))
        self.assertFalse(self.swarm.complete(self.result(job)))
        self.assertIsNone(self.swarm.claim("two", "two"))

    def test_retry_limit_is_bounded(self):
        self.swarm.submit(self.folder)
        for _ in range(MAX_ATTEMPTS):
            self.assertIsNotNone(self.swarm.claim("one", "one"))
            self.now += LEASE_SECONDS + 1
        self.assertIsNone(self.swarm.claim("two", "two"))
        self.assertEqual(self.swarm.batches()[0]["counts"]["failed"], 1)

    def test_rejects_wrong_digest_without_accepting_result(self):
        self.swarm.submit(self.folder)
        job = self.swarm.claim("one", "one")
        result = self.result(job)
        result["result"]["sha256"] = "bad"
        with self.assertRaises(ValueError):
            self.swarm.complete(result)
        self.assertTrue(self.swarm.active("one"))

    def test_duplicates_and_unicode_match_reference(self):
        (self.folder / "copy.txt").write_text("hello world\nhello", encoding="utf-8")
        (self.folder / "uk.txt").write_text("Привіт світе\nПривіт", encoding="utf-8")
        (self.folder / "bad.txt").write_bytes(b"\xff\xfe")
        batch, count, skipped = self.swarm.submit(self.folder)
        self.assertEqual((count, len(skipped)), (3, 1))
        while (job := self.swarm.claim("one", "one")):
            self.swarm.complete(self.result(job))
        _, report = self.swarm.export(batch)
        self.assertEqual(report["words"], 9)
        self.assertEqual(report["duplicates"], [["a.txt", "copy.txt"]])

    def test_submission_is_a_snapshot_not_a_later_remote_path_read(self):
        self.swarm.submit(self.folder)
        (self.folder / "a.txt").write_text("changed after submission")
        job = self.swarm.claim("one", "one")
        self.assertEqual(analyze(job["payload"])["words"], 3)

    def test_concurrent_claims_do_not_duplicate_chunk(self):
        self.swarm.submit(self.folder)
        claims = []
        threads = [threading.Thread(target=lambda i=i: claims.append(self.swarm.claim(str(i), str(i)))) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(job is not None for job in claims), 1)


class ControllerIntegrationTests(unittest.TestCase):
    def test_real_http_two_worker_loops_reports_and_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            for i in range(12):
                (corpus / f"{i}.txt").write_text(f"Group {i % 6}\n" + "one two three\n" * 100, encoding="utf-8")
            app = ClusterController(0, "integration-only", root / "controller")
            app.start()
            server = f"http://127.0.0.1:{app.port}"
            stop = threading.Event()
            threads = []
            try:
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    api_request(server, "incorrect", "GET", "/health")
                self.assertEqual(rejected.exception.code, 401)
                batch, _, _ = app.swarm.submit(corpus)
                def fixed_snapshot(node_id, name, docker, data_dir=None):
                    return {"node_id": node_id, "name": name, "system": "Test", "cpu_total": 4,
                            "cpu_usage": 10, "ram_total": 8, "ram_free": 6,
                            "disk_total": 100, "disk_free": 80, "docker": False,
                            "sharing": {"ready": True, "cores": 1, "ram": 1, "cpu_usage": 10, "ac": True, "reason": "Ready: test fixture"}}
                with patch("cluster.worker.show_worker"), patch("cluster.worker.docker_available", return_value=False), patch("cluster.worker.snapshot", side_effect=fixed_snapshot), patch("cluster.worker.cpu_percent", return_value=10), patch("cluster.worker.memory_stats", return_value=(8, 6)), patch("cluster.worker.power_status", return_value={"ac": True, "battery": 100}), contextlib.redirect_stdout(io.StringIO()):
                    for i in range(2):
                        worker_dir = root / f"worker-{i}"
                        worker_dir.mkdir()
                        (worker_dir / "sharing.json").write_text(json.dumps(DEFAULT_POLICY))
                        thread = threading.Thread(target=run_worker, args=(server, "integration-only", f"Worker {i}", worker_dir, stop, 0.05))
                        threads.append(thread)
                        thread.start()
                    deadline = time.time() + 15
                    while time.time() < deadline:
                        if app.swarm.batches()[0]["counts"].get("done") == 12:
                            break
                        time.sleep(0.05)
                    stop.set()
                    for thread in threads:
                        thread.join(timeout=12)
                    _, report = app.swarm.export(batch)
                    self.assertTrue(report["complete"])
                    self.assertEqual(report["words"], 12 * 301)
                    self.assertEqual(len(report["duplicates"]), 6)
                    self.assertEqual(len(report["workers"]), 2)
                    show_dashboard(app, clear_screen=False)
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=12)
                app.stop()


if __name__ == "__main__":
    unittest.main()
