import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from whichsdk.detect.build import BuildResult
from whichsdk.detect.verdict import Detection

Status = Literal["pending", "running", "done", "failed"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
    name TEXT PRIMARY KEY,
    yaml TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    study TEXT NOT NULL REFERENCES studies (name),
    task_index INTEGER NOT NULL,
    agent TEXT NOT NULL,
    repeat INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    started_at TEXT,
    agent_started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    cost_usd REAL,
    duration_s REAL,
    transcript_path TEXT,
    model TEXT,
    claude_code_version TEXT,
    PRIMARY KEY (id, attempt)
);

CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (run_id, attempt) REFERENCES runs (id, attempt)
);

CREATE TABLE IF NOT EXISTS results (
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('picked', 'installed_unused', 'multiple', 'undetermined')),
    picked_competitor TEXT,
    added TEXT NOT NULL,
    removed TEXT NOT NULL,
    imported TEXT NOT NULL,
    switches TEXT NOT NULL,
    searched INTEGER NOT NULL,
    build_passed INTEGER,
    build_log_path TEXT,
    PRIMARY KEY (run_id, attempt),
    FOREIGN KEY (run_id, attempt) REFERENCES runs (id, attempt)
);
"""


@dataclass(frozen=True)
class Run:
    """One attempt at a run: a row of the runs table."""

    id: str
    attempt: int
    study: str
    task_index: int
    agent: str
    repeat: int
    status: Status
    started_at: str | None
    agent_started_at: str | None
    finished_at: str | None
    exit_code: int | None
    cost_usd: float | None
    duration_s: float | None
    transcript_path: str | None
    model: str | None
    claude_code_version: str | None


@dataclass(frozen=True)
class Spend:
    """What a study has cost, and how many of its attempts had no reported cost."""

    total_usd: float
    unreported_attempts: int


class Store:
    """SQLite record of studies, run attempts and their events."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()

    def save_study(self, name: str, yaml_text: str) -> None:
        """Keep the study file as written, replacing any earlier copy."""
        with self._db:
            self._db.execute(
                "INSERT INTO studies (name, yaml) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET yaml = excluded.yaml",
                (name, yaml_text),
            )

    def next_attempt(self, study: str, task_index: int, agent: str, repeat: int) -> Run:
        """Return the attempt to run next; a done run comes back unchanged."""
        run_id = f"{study}-t{task_index}-{agent}-r{repeat}"
        with self._db:
            latest = self._latest_attempt(run_id)
            if latest is not None and latest.status in ("pending", "done"):
                return latest
            if latest is not None and latest.status == "running":
                self._db.execute(
                    "UPDATE runs SET status = 'failed' WHERE id = ? AND attempt = ?",
                    (latest.id, latest.attempt),
                )
                self._insert_event(latest.id, latest.attempt, "run_interrupted", {})
            attempt = 1 if latest is None else latest.attempt + 1
            self._db.execute(
                "INSERT INTO runs (id, attempt, study, task_index, agent, repeat) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, attempt, study, task_index, agent, repeat),
            )
        return self.get_run(run_id, attempt)

    def get_run(self, run_id: str, attempt: int) -> Run:
        """Return one attempt of a run."""
        row = self._db.execute(
            "SELECT * FROM runs WHERE id = ? AND attempt = ?", (run_id, attempt)
        ).fetchone()
        return Run(**row)

    def study_attempts(self, study: str) -> list[Run]:
        """Return every attempt of a study, in run id and attempt order."""
        rows = self._db.execute(
            "SELECT * FROM runs WHERE study = ? ORDER BY id, attempt", (study,)
        ).fetchall()
        return [Run(**row) for row in rows]

    def study_spend(self, study: str, unreported_usd: float) -> Spend:
        """Total cost of every attempt, failed ones included.

        An attempt that started its agent but reported no cost counts as unreported_usd.
        """
        recorded, unreported = self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0), "
            "COALESCE(SUM(cost_usd IS NULL AND agent_started_at IS NOT NULL), 0) "
            "FROM runs WHERE study = ?",
            (study,),
        ).fetchone()
        return Spend(recorded + unreported * unreported_usd, unreported)

    def start_run(self, run: Run, model: str) -> None:
        """Move a pending attempt to running and record the model it will use."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE runs SET status = 'running', started_at = ?, model = ? "
                "WHERE id = ? AND attempt = ? AND status = 'pending'",
                (_now(), model, run.id, run.attempt),
            )
        _expect_one_row(cursor, run, "start")

    def begin_agent(self, run: Run, version: str) -> None:
        """Record the agent's version and that it is about to run, and so may spend money."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE runs SET claude_code_version = ?, agent_started_at = ? "
                "WHERE id = ? AND attempt = ? AND status = 'running'",
                (version, _now(), run.id, run.attempt),
            )
        _expect_one_row(cursor, run, "begin the agent of")

    def record_result(
        self,
        run: Run,
        exit_code: int,
        cost_usd: float | None,
        duration_s: float,
        transcript_path: Path,
    ) -> None:
        """Save what the agent produced on a running attempt."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE runs SET exit_code = ?, cost_usd = ?, duration_s = ?, transcript_path = ? "
                "WHERE id = ? AND attempt = ? AND status = 'running'",
                (exit_code, cost_usd, duration_s, transcript_path.as_posix(), run.id, run.attempt),
            )
        _expect_one_row(cursor, run, "record a result for")

    def finish_run(self, run: Run, status: Literal["done", "failed"]) -> None:
        """Move a running attempt to done or failed."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE runs SET status = ?, finished_at = ? "
                "WHERE id = ? AND attempt = ? AND status = 'running'",
                (status, _now(), run.id, run.attempt),
            )
        _expect_one_row(cursor, run, "finish")

    def move_transcript(self, run_id: str, attempt: int, transcript_path: Path) -> None:
        """Point an attempt that has a transcript at the place its files were moved to."""
        with self._db:
            self._db.execute(
                "UPDATE runs SET transcript_path = ? "
                "WHERE id = ? AND attempt = ? AND transcript_path IS NOT NULL",
                (transcript_path.as_posix(), run_id, attempt),
            )

    def add_event(self, run: Run, kind: str, payload: dict[str, Any]) -> None:
        """Append an event to the attempt's log."""
        with self._db:
            self._insert_event(run.id, run.attempt, kind, payload)

    def build_passed(self, run: Run) -> bool | None:
        """Return whether the attempt's build passed, or None if no build was recorded."""
        row = self._db.execute(
            "SELECT payload FROM events WHERE run_id = ? AND attempt = ? "
            "AND kind = 'build_finished' ORDER BY rowid DESC LIMIT 1",
            (run.id, run.attempt),
        ).fetchone()
        return None if row is None else json.loads(row["payload"])["passed"]

    def save_results(self, run: Run, detection: Detection, build: BuildResult | None) -> None:
        """Keep what the detectors found for an attempt, replacing any earlier results."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO results (run_id, attempt, verdict, picked_competitor, "
                "added, removed, imported, switches, searched, build_passed, build_log_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.attempt,
                    detection.verdict,
                    detection.picked,
                    json.dumps(detection.added),
                    json.dumps(detection.removed),
                    json.dumps(detection.imported),
                    json.dumps([asdict(switch) for switch in detection.switches]),
                    int(detection.searched),
                    None if build is None else int(build.passed),
                    None if build is None else build.log_path.as_posix(),
                ),
            )

    def move_build_log(self, run_id: str, attempt: int, log_path: Path) -> None:
        """Point an attempt's results at the place its build log was moved to."""
        with self._db:
            self._db.execute(
                "UPDATE results SET build_log_path = ? "
                "WHERE run_id = ? AND attempt = ? AND build_log_path IS NOT NULL",
                (log_path.as_posix(), run_id, attempt),
            )

    def _latest_attempt(self, run_id: str) -> Run | None:
        row = self._db.execute(
            "SELECT * FROM runs WHERE id = ? ORDER BY attempt DESC LIMIT 1", (run_id,)
        ).fetchone()
        return None if row is None else Run(**row)

    def _insert_event(self, run_id: str, attempt: int, kind: str, payload: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO events (run_id, attempt, kind, payload) VALUES (?, ?, ?, ?)",
            (run_id, attempt, kind, json.dumps(payload)),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _expect_one_row(cursor: sqlite3.Cursor, run: Run, action: str) -> None:
    if cursor.rowcount != 1:
        raise ValueError(f"cannot {action} attempt {run.attempt} of {run.id} in its current state")
