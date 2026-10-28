import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from whichsdk import cli
from whichsdk.detect.verdict import Detection
from whichsdk.store import Run, Store

STUDY: dict[str, object] = {
    "name": "demo",
    "category": "auth",
    "competitors": [{"name": "Clerk", "npm_packages": ["@clerk/nextjs"]}],
    "starter_template": "starter",
    "tasks": ["alpha", "beta", "gamma"],
    "agents": [{"name": "claude_code", "model": "test-model"}],
    "repeats": 2,
    "timeout_seconds": 600,
    "max_turns": 10,
    "run_budget_usd": 1.0,
    "study_budget_usd": 20.0,
}
PICKED = Detection("picked", "Clerk", None, {"@clerk/nextjs": "Clerk"}, {}, {}, [], False)
NO_SDK = Detection("no_sdk", None, None, {}, {}, {}, [], False)
RAN_OUT_OF_TURNS = Detection("undetermined", None, "max_turns", {}, {}, {}, [], False)
CRASHED = Detection("undetermined", None, "run_failed", {}, {}, {}, [], False)
ALL_ZERO = [
    "Verdicts of the newest attempt of each run:",
    f"  {'picked':<16}  0",
    f"  {'installed_unused':<16}  0",
    f"  {'multiple':<16}  0",
    f"  {'other':<16}  0",
    f"  {'no_sdk':<16}  0",
    f"  {'undetermined':<16}  0",
]


def status() -> Result:
    return CliRunner().invoke(cli.app, ["status", "study.yaml"])


def dump() -> list[str]:
    with closing(sqlite3.connect(cli.DB_PATH)) as connection:
        return list(connection.iterdump())


def finish(
    store: Store,
    task: int,
    repeat: int,
    exit_code: int,
    cost: float | None,
    detection: Detection | None,
    study: str = "demo",
) -> Run:
    run = store.next_attempt(study, task, "claude_code", repeat)
    store.start_run(run, "test-model")
    store.begin_agent(run, "9.9.9")
    store.record_result(run, exit_code, cost, 3.0, Path("runs") / run.id / "transcript.jsonl")
    store.finish_run(run, "done" if exit_code == 0 else "failed")
    if detection is not None:
        store.save_results(run, detection, None)
    return run


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    monkeypatch.chdir(tmp_path)
    Path("study.yaml").write_text(yaml.safe_dump(STUDY), encoding="utf-8")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("status must not make an agent, start a container or remove one")

    monkeypatch.setattr(cli, "ClaudeCodeAgent", forbidden)
    monkeypatch.setattr(cli, "start_sandbox", forbidden)
    monkeypatch.setattr(cli, "remove_container", forbidden)
    with closing(Store(cli.DB_PATH)) as saved:
        saved.save_study("demo", "name: demo")
        yield saved


def test_a_study_that_never_ran_is_all_pending_and_status_makes_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("study.yaml").write_text(yaml.safe_dump(STUDY), encoding="utf-8")

    outcome = status()

    assert outcome.exit_code == 0, outcome.output
    assert outcome.output.splitlines() == [
        "demo: 6 runs (3 tasks x 2 repeats x 1 agent(s))",
        "done 0, failed 0, running 0, pending 6",
        "$0.00 of $20.00 spent on demo",
        *ALL_ZERO,
    ]
    assert not cli.DB_PATH.exists()


def test_runs_are_counted_by_their_newest_attempt(store: Store) -> None:
    finish(store, 0, 0, 0, 0.3, PICKED)
    finish(store, 1, 0, 1, 0.4, CRASHED)
    finish(store, 1, 0, 0, 0.2, NO_SDK)
    finish(store, 2, 0, 1, 0.9, RAN_OUT_OF_TURNS)
    running = store.next_attempt("demo", 0, "claude_code", 1)
    store.start_run(running, "test-model")
    store.begin_agent(running, "9.9.9")
    store.next_attempt("demo", 1, "claude_code", 1)

    outcome = status()

    assert outcome.exit_code == 0, outcome.output
    assert outcome.output.splitlines() == [
        "demo: 6 runs (3 tasks x 2 repeats x 1 agent(s))",
        "done 2, failed 1, running 1, pending 2",
        "$2.80 of $20.00 spent on demo (1 attempt(s) with no reported cost counted at $1.00 each)",
        "Verdicts of the newest attempt of each run:",
        f"  {'picked':<16}  1",
        f"  {'installed_unused':<16}  0",
        f"  {'multiple':<16}  0",
        f"  {'other':<16}  0",
        f"  {'no_sdk':<16}  1",
        f"  {'undetermined':<16}  1",
    ]


def test_a_retry_that_has_not_started_makes_its_run_pending_again(store: Store) -> None:
    finish(store, 0, 0, 1, 0.4, CRASHED)
    store.next_attempt("demo", 0, "claude_code", 0)

    outcome = status()

    assert outcome.output.splitlines()[1] == "done 0, failed 0, running 0, pending 6"
    assert outcome.output.splitlines()[3:] == ALL_ZERO


def test_other_studies_in_the_same_database_are_left_out(store: Store) -> None:
    store.save_study("other", "name: other")
    finish(store, 0, 0, 0, 5.0, PICKED, study="other")
    finish(store, 0, 0, 0, 0.5, NO_SDK)

    outcome = status()

    assert outcome.output.splitlines()[1:3] == [
        "done 1, failed 0, running 0, pending 5",
        "$0.50 of $20.00 spent on demo",
    ]
    assert f"  {'picked':<16}  0" in outcome.output
    assert f"  {'no_sdk':<16}  1" in outcome.output


def test_runs_outside_the_study_file_are_not_counted_as_its_runs(store: Store) -> None:
    finish(store, 0, 4, 0, 0.5, NO_SDK)

    outcome = status()

    assert outcome.output.splitlines()[1] == "done 0, failed 0, running 0, pending 6"


def test_status_changes_nothing(store: Store) -> None:
    finish(store, 0, 0, 0, 0.3, PICKED)
    finish(store, 1, 0, 1, None, CRASHED)
    before = dump()

    status()

    assert dump() == before
    assert not Path("runs").exists()
