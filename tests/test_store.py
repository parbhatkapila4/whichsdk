import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from whichsdk.store import Run, Spend, Store

RUN_ID = "demo-t0-claude_code-r0"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with closing(Store(db_path)) as store:
        store.save_study("demo", "name: demo")
        yield store


def next_attempt(store: Store, repeat: int = 0) -> Run:
    return store.next_attempt("demo", 0, "claude_code", repeat)


def run_agent(store: Store, run: Run, exit_code: int, cost: float | None) -> None:
    store.start_run(run, "test-model")
    store.begin_agent(run, "1.2.3")
    store.record_result(run, exit_code, cost, 3.0, Path("runs") / run.id / "transcript.jsonl")
    store.finish_run(run, "done" if exit_code == 0 else "failed")


def fail_before_the_agent(store: Store, run: Run) -> None:
    store.start_run(run, "test-model")
    store.finish_run(run, "failed")


def query(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute(sql).fetchall()


def test_the_first_attempt_is_pending(store: Store) -> None:
    run = store.next_attempt("demo", 2, "claude_code", 1)

    assert (run.id, run.attempt) == ("demo-t2-claude_code-r1", 1)
    assert (run.task_index, run.agent, run.repeat) == (2, "claude_code", 1)
    assert run.status == "pending"
    assert (run.started_at, run.model, run.claude_code_version) == (None, None, None)


def test_a_pending_attempt_is_returned_again_instead_of_a_new_one(
    store: Store, db_path: Path
) -> None:
    first = next_attempt(store)

    again = next_attempt(store)

    assert again == first
    assert len(query(db_path, "SELECT 1 FROM runs")) == 1


def test_a_failed_attempt_gets_a_new_attempt_after_it_and_stays_on_record(store: Store) -> None:
    run_agent(store, next_attempt(store), exit_code=1, cost=0.4)

    second = next_attempt(store)

    assert (second.id, second.attempt, second.status) == (RUN_ID, 2, "pending")
    assert second.cost_usd is None
    kept = store.get_run(RUN_ID, 1)
    assert (kept.status, kept.exit_code, kept.cost_usd) == ("failed", 1, 0.4)
    assert kept.transcript_path == f"runs/{RUN_ID}/transcript.jsonl"


def test_every_attempt_keeps_its_own_cost(store: Store, db_path: Path) -> None:
    for exit_code, cost in [(1, 0.4), (1, 0.3), (0, 0.2)]:
        run_agent(store, next_attempt(store), exit_code, cost)

    rows = query(db_path, "SELECT attempt, status, cost_usd FROM runs ORDER BY attempt")

    assert rows == [(1, "failed", 0.4), (2, "failed", 0.3), (3, "done", 0.2)]
    assert store.study_spend("demo", 1.0).total_usd == pytest.approx(0.9)


def test_a_done_run_comes_back_unchanged_without_a_new_attempt(store: Store, db_path: Path) -> None:
    run_agent(store, next_attempt(store), exit_code=0, cost=0.5)

    run = next_attempt(store)

    assert (run.attempt, run.status) == (1, "done")
    assert len(query(db_path, "SELECT 1 FROM runs")) == 1


def test_an_attempt_left_running_is_closed_out_as_failed_and_kept(
    store: Store, db_path: Path
) -> None:
    crashed = next_attempt(store)
    store.start_run(crashed, "test-model")
    store.begin_agent(crashed, "1.2.3")

    retry = next_attempt(store)

    assert (retry.attempt, retry.status) == (2, "pending")
    old = store.get_run(RUN_ID, 1)
    assert (old.status, old.finished_at, old.cost_usd) == ("failed", None, None)
    assert old.agent_started_at is not None
    assert query(db_path, "SELECT run_id, attempt, kind FROM events") == [
        (RUN_ID, 1, "run_interrupted")
    ]


def test_a_crash_is_recovered_by_the_next_process(db_path: Path) -> None:
    with closing(Store(db_path)) as first:
        first.save_study("demo", "name: demo")
        first.start_run(next_attempt(first), "test-model")

    with closing(Store(db_path)) as second:
        retry = next_attempt(second)

        assert retry.attempt == 2
        assert second.get_run(RUN_ID, 1).status == "failed"
        second.start_run(retry, "test-model")


def test_starting_an_attempt_records_the_model(store: Store) -> None:
    store.start_run(next_attempt(store), "claude-sonnet-5-5")

    started = store.get_run(RUN_ID, 1)

    assert (started.status, started.model) == ("running", "claude-sonnet-5-5")
    assert started.started_at is not None


def test_beginning_the_agent_records_the_version(store: Store) -> None:
    run = next_attempt(store)
    store.start_run(run, "test-model")

    store.begin_agent(run, "2.1.310")

    begun = store.get_run(RUN_ID, 1)
    assert begun.claude_code_version == "2.1.310"
    assert begun.agent_started_at is not None


def test_an_attempt_cannot_be_started_twice_or_after_it_is_done(store: Store) -> None:
    run = next_attempt(store)
    store.start_run(run, "test-model")

    with pytest.raises(ValueError, match="cannot start"):
        store.start_run(run, "test-model")

    store.finish_run(run, "done")
    with pytest.raises(ValueError, match="cannot start"):
        store.start_run(run, "test-model")
    assert store.get_run(RUN_ID, 1).status == "done"


def test_only_a_running_attempt_can_begin_its_agent_take_a_result_or_finish(
    store: Store,
) -> None:
    run = next_attempt(store)

    with pytest.raises(ValueError, match="cannot begin the agent of"):
        store.begin_agent(run, "1.0")
    with pytest.raises(ValueError, match="cannot record a result for"):
        store.record_result(run, 0, None, 1.0, Path("transcript.jsonl"))
    with pytest.raises(ValueError, match="cannot finish"):
        store.finish_run(run, "done")


def test_spend_adds_up_failed_attempts_too(store: Store) -> None:
    run_agent(store, next_attempt(store), exit_code=1, cost=0.4)
    run_agent(store, next_attempt(store), exit_code=0, cost=0.3)

    spend = store.study_spend("demo", 1.0)

    assert spend.total_usd == pytest.approx(0.7)
    assert spend.unreported_attempts == 0


def test_spend_counts_an_agent_run_without_a_cost_at_the_given_amount(store: Store) -> None:
    run_agent(store, next_attempt(store), exit_code=124, cost=None)
    run_agent(store, next_attempt(store), exit_code=0, cost=0.25)

    spend = store.study_spend("demo", 1.0)

    assert (spend.total_usd, spend.unreported_attempts) == (1.25, 1)
    assert store.get_run(RUN_ID, 1).cost_usd is None


def test_spend_ignores_attempts_that_never_reached_the_agent(store: Store) -> None:
    next_attempt(store, repeat=0)
    store.start_run(next_attempt(store, repeat=1), "test-model")
    fail_before_the_agent(store, next_attempt(store, repeat=2))

    assert store.study_spend("demo", 1.0) == Spend(0.0, 0)


def test_spend_counts_an_attempt_whose_agent_is_still_running(store: Store) -> None:
    run = next_attempt(store)
    store.start_run(run, "test-model")
    store.begin_agent(run, "1.2.3")

    assert store.study_spend("demo", 1.0) == Spend(1.0, 1)


def test_spend_is_kept_per_study(store: Store) -> None:
    store.save_study("other", "name: other")
    run_agent(store, store.next_attempt("other", 0, "claude_code", 0), exit_code=0, cost=2.0)
    run_agent(store, next_attempt(store), exit_code=0, cost=0.5)

    assert store.study_spend("demo", 1.0).total_usd == 0.5
    assert store.study_spend("other", 1.0).total_usd == 2.0
    assert store.study_spend("never-run", 1.0) == Spend(0.0, 0)


def test_moving_a_transcript_updates_only_attempts_that_have_one(store: Store) -> None:
    run_agent(store, next_attempt(store), exit_code=1, cost=0.1)
    fail_before_the_agent(store, next_attempt(store))

    store.move_transcript(RUN_ID, 1, Path("runs") / RUN_ID / "attempts" / "1" / "t.jsonl")
    store.move_transcript(RUN_ID, 2, Path("runs") / RUN_ID / "attempts" / "2" / "t.jsonl")

    assert store.get_run(RUN_ID, 1).transcript_path == f"runs/{RUN_ID}/attempts/1/t.jsonl"
    assert store.get_run(RUN_ID, 2).transcript_path is None


def test_a_run_needs_a_saved_study(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.next_attempt("unknown", 0, "claude_code", 0)


def test_saving_a_study_again_replaces_the_text(store: Store, db_path: Path) -> None:
    store.save_study("demo", "name: demo\nrepeats: 2")

    rows = query(db_path, "SELECT name, yaml FROM studies")

    assert rows == [("demo", "name: demo\nrepeats: 2")]


def test_events_belong_to_an_attempt_and_keep_their_order(store: Store, db_path: Path) -> None:
    run_agent(store, next_attempt(store), exit_code=1, cost=0.1)
    second = next_attempt(store)
    store.add_event(second, "run_start", {"prompt": "add login", "max_turns": 60})
    store.add_event(second, "run_error", {"message": "docker exited 1"})

    rows = query(db_path, "SELECT run_id, attempt, kind, payload FROM events ORDER BY rowid")

    assert [(run, attempt, kind) for run, attempt, kind, _ in rows] == [
        (RUN_ID, 2, "run_start"),
        (RUN_ID, 2, "run_error"),
    ]
    assert json.loads(str(rows[0][3])) == {"prompt": "add login", "max_turns": 60}
