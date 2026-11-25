import tempfile
import time
import unittest
from pathlib import Path

from cluster.controller import ClusterController, select_node
from cluster.security import TrustStore, WorkerSession
from cluster.system import DEFAULT_POLICY, save_policy
from cluster.tasks import Node, SwarmQueue
from cluster.worker import ClusterWorker, _run_python_builtin


class ClusterTests(unittest.TestCase):
    def test_invite_single_use_and_revoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TrustStore(Path(tmp))
            code = store.create_invite(ttl=60)
            issued = store.redeem_invite(code, "PC-1")
            self.assertEqual(store.authenticate(issued["token"]), issued["node_id"])
            with self.assertRaises(ValueError):
                store.redeem_invite(code, "PC-2")
            self.assertTrue(store.revoke(issued["node_id"]))
            self.assertIsNone(store.authenticate(issued["token"]))

    def test_short_code_and_access_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctrl = ClusterController(0, root / "ctrl")
            ctrl.start()
            try:
                code, _ = ctrl.issue_invite_pair("127.0.0.1")
                self.assertEqual(len(code), 6)
                s1 = WorkerSession.pair_with_code("127.0.0.1", ctrl.port, ctrl.fingerprint, code, "ShortPC", root / "w1")
                self.assertEqual(s1.name, "ShortPC")

                req_id = ctrl.trust.create_access_request("AskPC", "127.0.0.1")
                self.assertEqual(len(ctrl.trust.list_pending_requests()), 1)
                self.assertTrue(ctrl.approve_request(req_id))
                st = ctrl.trust.get_request_status(req_id)
                self.assertEqual(st["status"], "approved")
            finally:
                ctrl.stop()

    def test_scheduler_best_fit_and_runtime(self):
        now = time.time()
        n_big = Node("n1", "Big", "10.0.0.1", "Win", 8, 10, 16, 12.0, 200, 100, runtimes=["python"], last_seen=now)
        n_fit = Node("n2", "Fit", "10.0.0.2", "Win", 4, 10, 8, 3.0, 100, 50, runtimes=["python", "node"], last_seen=now)
        perms = {"n1": ["exec"], "n2": ["exec"]}
        chosen = select_node([n_big, n_fit], cpu=2, ram=2.0, disk=10, kind="exec", permissions=perms, req_runtime="node")
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.node_id, "n2")

    def test_swarm_lease_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            (docs / "a.txt").write_text("hello world", encoding="utf-8")
            (docs / "b.txt").write_text("hello world", encoding="utf-8")

            clock_now = [100.0]
            q = SwarmQueue(root / "data", clock=lambda: clock_now[0])
            bid, count, _ = q.submit_folder(docs)
            self.assertEqual(count, 2)

            c1 = q.claim("w1")
            self.assertIsNotNone(c1)
            clock_now[0] += 25.0
            c2 = q.claim("w2")
            self.assertEqual(c2["job_id"], c1["job_id"])
            q.close()

    def test_real_controller_worker_exec_and_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctrl = ClusterController(0, root / "ctrl")
            ctrl.start()
            w_dir = root / "w1"
            save_policy(w_dir, {**DEFAULT_POLICY, "reserve_ram_gb": 0, "max_host_cpu": 100, "minimum_battery": 0})
            invite = ctrl.issue_invitation("127.0.0.1")
            session = WorkerSession.pair(invite, "Worker-Test", w_dir)
            worker = ClusterWorker(session, w_dir, poll_seconds=0.1)
            worker.start()

            try:
                deadline = time.monotonic() + 10
                while not ctrl.nodes and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(len(ctrl.nodes), 1)

                script = root / "hello.py"
                script.write_text("print('EXEC_OK', 2 + 2)", encoding="utf-8")
                job = ctrl.create_job("exec", 1, 0.1, 0.1, target_path=script)
                self.assertIsNotNone(job)
                while job.state in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(job.state, "done")
                self.assertIn("EXEC_OK 4", job.output)

                bid, cnt = ctrl.submit_sample()
                self.assertEqual(cnt, 24)
                while time.monotonic() < deadline:
                    if ctrl.swarm.batches()[0]["counts"]["done"] == 24:
                        break
                    time.sleep(0.05)
                _, rep = ctrl.swarm.export(bid)
                self.assertTrue(rep["complete"])
                self.assertEqual(len(rep["duplicates"]), 12)

                map_dir = root / "map_in"
                map_dir.mkdir()
                (map_dir / "x.txt").write_text("alpha", encoding="utf-8")
                (map_dir / "y.txt").write_text("beta", encoding="utf-8")
                mapper = root / "mapper.py"
                mapper.write_text("import sys, pathlib\nprint('MAP:', pathlib.Path(sys.argv[1]).read_text().upper())", encoding="utf-8")
                mbid, mcnt = ctrl.swarm.submit_map(map_dir, str(mapper))
                self.assertEqual(mcnt, 2)
                while time.monotonic() < deadline:
                    mb = [b for b in ctrl.swarm.batches() if b["id"] == mbid][0]
                    if mb["counts"]["done"] == 2:
                        break
                    time.sleep(0.05)
                _, mrep = ctrl.swarm.export(mbid)
                self.assertTrue(mrep["complete"])
                outputs = " ".join(r["output"] for r in mrep["results"])
                self.assertIn("MAP: ALPHA", outputs)
                self.assertIn("MAP: BETA", outputs)

                ex_dir = Path(__file__).resolve().parents[1] / "examples"
                st, out = _run_python_builtin(ex_dir / "monte_carlo_pi.py", ex_dir)
                self.assertEqual(st, "done")
                self.assertIn("Monte Carlo Pi", out)

                sample_txt = root / "sample.txt"
                sample_txt.write_text("one two three", encoding="utf-8")
                st2, out2 = _run_python_builtin(ex_dir / "text_stats_map.py", root, [sample_txt.name])
                self.assertEqual(st2, "done")
                self.assertIn("3 words", out2)
            finally:
                worker.stop()
                ctrl.stop()


if __name__ == "__main__":
    unittest.main()
