import json
import sqlite3
import threading
from pathlib import Path

from .models import Job


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                )
                """
            )

    def save_job(self, job: Job):
        payload = json.dumps(job.to_dict(), ensure_ascii=False)
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO jobs(job_id, payload) VALUES (?, ?)",
                (job.job_id, payload),
            )

    def load_jobs(self) -> dict[str, Job]:
        with self.lock:
            rows = self.connection.execute("SELECT payload FROM jobs").fetchall()
        result = {}
        for row in rows:
            data = json.loads(row["payload"])
            job = Job(**data)
            if job.state in ("queued", "running"):
                job.state = "failed"
                job.output = "Controller restarted before the job completed."
            result[job.job_id] = job
        return result

    def add_event(self, created_at: str, level: str, message: str):
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO events(created_at, level, message) VALUES (?, ?, ?)",
                (created_at, level, message),
            )

    def recent_events(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT created_at, level, message FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

