
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cluster.controller import ClusterController
from cluster.sharing import DEFAULT_POLICY
from cluster.worker import run_worker
from cluster.trust import NodeSession


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        session = NodeSession.load(Path(sys.argv[3]))
        run_worker(session.server, session, session.name, Path(sys.argv[3]), poll_seconds=0.1, display=False)
        return
    with tempfile.TemporaryDirectory(prefix="borrowed-compute-demo-") as directory:
        root = Path(directory)
        corpus = root / "input"
        corpus.mkdir()
        for index in range(24):
            (corpus / f"file-{index:02}.txt").write_text(f"Group {index % 12}\n" + "one two three\n" * 500, encoding="utf-8")
        app = ClusterController(0, '', root / "controller", secure=True)
        app.start()
        processes, logs = [], []
        try:
            for index in range(2):
                data = root / f"worker-{index}"
                data.mkdir()
                policy = {**DEFAULT_POLICY, "reserve_ram_gb": 0, "max_host_cpu": 100, "minimum_battery": 0}
                (data / "sharing.json").write_text(json.dumps(policy))
                NodeSession.pair(app.issue_invitation('127.0.0.1'), f'Laptop-{index + 1}', data)
                log = (root / f"worker-{index}.log").open("w", encoding="utf-8")
                logs.append(log)
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker",
                                            f"https://127.0.0.1:{app.port}", str(data), f"Laptop-{index + 1}"],
                                           stdout=log, stderr=subprocess.STDOUT,
                                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                processes.append(process)
            deadline = time.monotonic() + 40
            while len(app.nodes) < 2 and time.monotonic() < deadline:
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("A worker exited; inspect demo output")
                time.sleep(0.1)
            if len(app.nodes) != 2:
                raise RuntimeError("Workers did not register")
            print("Two independent worker processes paired over pinned TLS with real host telemetry.")
            batch, _, _ = app.swarm.submit(corpus)
            while time.monotonic() < deadline:
                counts = app.swarm.batches()[0]["counts"]
                if counts.get("done") == 24:
                    break
                time.sleep(0.1)
            _, report = app.swarm.export(batch)
            if not report["complete"] or report["words"] != 24 * 1501 or len(report["duplicates"]) != 12 or len(report["workers"]) != 2:
                raise AssertionError(f"Unexpected report: {report['files']} files / {report['workers']}")
            print(json.dumps({key: report[key] for key in ("complete", "files", "words", "workers")}, indent=2))
            print("PASS: 24 files, 12 duplicate groups, two real processes, exact reference counts.")
        except Exception:
            for log in logs:
                log.flush()
            for path in root.glob("*.log"):
                print(path.read_text(encoding="utf-8", errors="replace")[-4000:])
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                process.wait(timeout=15)
            for log in logs:
                log.close()
            app.stop()


if __name__ == "__main__":
    main()
