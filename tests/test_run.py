import _thread
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from whichsdk import cli
from whichsdk.agents.base import Agent, AgentResult
from whichsdk.detect.verdict import Detection
from whichsdk.sandbox import ExecResult, Sandbox, SandboxError
from whichsdk.spec import StudySpec
from whichsdk.store import Store

STUDY: dict[str, object] = {
    "name": "demo",
    "category": "auth",
    "competitors": [{"name": "Clerk", "npm_packages": ["@clerk/nextjs"]}],
    "starter_template": "starter",
    "tasks": ["alpha", "beta"],
    "agents": [{"name": "claude_code", "model": "test-model"}],
    "repeats": 2,
    "timeout_seconds": 600,
    "max_turns": 10,
    "run_budget_usd": 1.0,
    "study_budget_usd": 20.0,
}


def run_id(task: int, repeat: int) -> str:
    return f"demo-t{task}-claude_code-r{repeat}"


@dataclass(frozen=True)
class Attempt:
    """What the fake agent does in one run."""

    exit_code: int = 0
    cost: float | None = 0.2
    installs: Mapping[str, str] = field(default_factory=dict)
    code: str = "export const answer = 42;"
    commands: Sequence[str] = ()
    ends: str = "success"


BUILDS_ITS_OWN = Attempt()
PICKS_CLERK = Attempt(
    installs={"@clerk/nextjs": "^6.0.0"},
    code='import { auth } from "@clerk/nextjs";',
    commands=("npm i @clerk/nextjs",),
)
INSTALLS_JOSE = Attempt(installs={"jose": "^6.0.0"}, commands=("npm i jose",))
CRASHES = Attempt(exit_code=1, cost=0.4, ends="error_during_execution")
RUNS_OUT_OF_TURNS = Attempt(exit_code=1, cost=0.9, ends="error_max_turns")


class Workspace(Sandbox):
    """A fake container whose dependencies and diff the fake agent edits."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.dependencies = {"next": "16.4.3"}
        self.diff = ""

    def exec(
        self, args: Sequence[str], env: Mapping[str, str] | None = None, check: bool = True
    ) -> ExecResult:
        return ExecResult(0, "built\n")

    def copy_out(self, names: Iterable[str], directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        package = json.dumps({"dependencies": self.dependencies})
        (directory / "package.json").write_text(package, encoding="utf-8")

    def changes_since_baseline(self, exclude: Iterable[str]) -> str:
        return self.diff


class ScriptedAgent(Agent):
    def __init__(self) -> None:
        self.scripts: dict[str, list[Attempt]] = {}
        self.started: list[str] = []
        self.running_rows: list[int] = []
        self.on_start: Callable[[Sandbox, int], None] | None = None
        self._lock = threading.Lock()

    def version(self, container: Sandbox) -> str:
        return "9.9.9"

    def run(self, container: Sandbox, prompt: str, timeout: int, max_turns: int) -> AgentResult:
        assert isinstance(container, Workspace)
        with self._lock:
            if not self.scripts.get(prompt):
                pytest.fail(f"the agent ran {prompt!r} when it should not have")
            attempt = self.scripts[prompt].pop(0)
            self.started.append(prompt)
            number = len(self.started)
        self.running_rows.append(count_running())
        if self.on_start is not None:
            self.on_start(container, number)
        container.dependencies.update(attempt.installs)
        container.diff = f"+++ b/lib/auth.ts\n+{attempt.code}\n"
        transcript = [
            line
            for position, command in enumerate(attempt.commands)
            for line in bash(f"c{position}", command)
        ]
        ending = {"type": "result", "subtype": attempt.ends, "total_cost_usd": attempt.cost}
        return AgentResult(attempt.exit_code, [*transcript, json.dumps(ending)], attempt.cost, 12.0)


class Harness:
    def __init__(self) -> None:
        self.agent = ScriptedAgent()
        self.agents_built: list[tuple[str, float]] = []
        self.sandboxes: list[str] = []
        self.removed: list[str] = []
        self.released: dict[str, threading.Event] = {}
        self.broken: set[str] = set()
        self.sandbox_delay = 0.0
        self.on_sandbox: Callable[[str], None] | None = None
        self._lock = threading.Lock()

    def script(self, prompt: str, *attempts: Attempt) -> None:
        self.agent.scripts.setdefault(prompt, []).extend(attempts)

    def make_agent(self, env_file: Path, model: str, run_budget_usd: float) -> Agent:
        self.agents_built.append((model, run_budget_usd))
        return self.agent

    @contextmanager
    def start_sandbox(self, name: str, starter: Path) -> Iterator[Sandbox]:
        if name in self.broken:
            raise SandboxError("docker exited 125: no space left on device")
        if self.on_sandbox is not None:
            self.on_sandbox(name)
        time.sleep(self.sandbox_delay)
        with self._lock:
            self.sandboxes.append(name)
            self.released[name] = threading.Event()
        yield Workspace(name)

    def remove_container(self, name: str) -> None:
        self.removed.append(name)
        if name in self.released:
            self.released[name].set()

    def hold_first(self, count: int, seconds: float = 0.3) -> None:
        barrier = threading.Barrier(count, timeout=10)

        def meet_and_wait(box: Sandbox, number: int) -> None:
            if number <= count:
                barrier.wait()
                time.sleep(seconds)

        self.agent.on_start = meet_and_wait

    def run(self, *args: str) -> Result:
        return CliRunner().invoke(cli.app, ["run", "study.yaml", *args])

    def status(self) -> Result:
        return CliRunner().invoke(cli.app, ["status", "study.yaml"])


def write_study(**overrides: object) -> None:
    Path("study.yaml").write_text(yaml.safe_dump({**STUDY, **overrides}), encoding="utf-8")


def bash(call_id: str, command: str) -> list[str]:
    use = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}
    done = {"type": "tool_result", "tool_use_id": call_id, "content": "ok", "is_error": False}
    return [
        json.dumps({"type": "assistant", "message": {"content": [use]}}),
        json.dumps({"type": "user", "message": {"content": [done]}}),
    ]


def db(sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(cli.DB_PATH)) as connection:
        return connection.execute(sql).fetchall()


def count_running() -> int:
    [(count,)] = db("SELECT COUNT(*) FROM runs WHERE status = 'running'")
    return int(str(count))


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.chdir(tmp_path)
    Path("starter").mkdir()
    write_study()
    harness = Harness()
    monkeypatch.setattr(cli, "ClaudeCodeAgent", harness.make_agent)
    monkeypatch.setattr(cli, "start_sandbox", harness.start_sandbox)
    monkeypatch.setattr(cli, "remove_container", harness.remove_container)
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    return harness


def test_every_task_and_repeat_is_run_repeat_by_repeat_with_one_line_for_each(
    harness: Harness,
) -> None:
    harness.script("alpha", PICKS_CLERK, PICKS_CLERK)
    harness.script("beta", BUILDS_ITS_OWN, INSTALLS_JOSE)

    outcome = harness.run()

    assert outcome.exit_code == 0, outcome.output
    assert outcome.output.splitlines() == [
        "demo: 4 run(s) to start, up to 1 at a time.",
        "task 0 repeat 0 claude_code: picked (Clerk), cost $0.20, total $0.20",
        "task 1 repeat 0 claude_code: no_sdk, cost $0.20, total $0.40",
        "task 0 repeat 1 claude_code: picked (Clerk), cost $0.20, total $0.60",
        "task 1 repeat 1 claude_code: other (jose), cost $0.20, total $0.80",
        "Ran 4 attempt(s): 4 done, 0 failed.",
    ]
    assert harness.agent.started == ["alpha", "beta", "alpha", "beta"]
    assert harness.agents_built == [("test-model", 1.0)]
    assert db("SELECT id, attempt, status, model FROM runs ORDER BY id") == [
        (run_id(task, repeat), 1, "done", "test-model") for task in (0, 1) for repeat in (0, 1)
    ]
    assert db("SELECT run_id, verdict, picked_competitor FROM results ORDER BY run_id") == [
        (run_id(0, 0), "picked", "Clerk"),
        (run_id(0, 1), "picked", "Clerk"),
        (run_id(1, 0), "no_sdk", None),
        (run_id(1, 1), "other", None),
    ]


def test_each_run_gets_its_own_prompt_container_and_files(harness: Harness) -> None:
    harness.script("alpha", BUILDS_ITS_OWN, BUILDS_ITS_OWN)
    harness.script("beta", BUILDS_ITS_OWN, BUILDS_ITS_OWN)

    harness.run()

    assert sorted(harness.sandboxes) == sorted(
        f"whichsdk-{run_id(t, r)}" for t in (0, 1) for r in (0, 1)
    )
    for task in (0, 1):
        for repeat in (0, 1):
            files = {path.name for path in (Path("runs") / run_id(task, repeat)).iterdir()}
            assert {"transcript.jsonl", "changes.diff", "build.log", "baseline"} <= files
    [(prompt,)] = db(
        f"SELECT json_extract(payload, '$.prompt') FROM events WHERE run_id = '{run_id(1, 1)}' "
        "AND kind = 'run_start'"
    )
    assert prompt == "beta"


def test_a_second_run_has_nothing_to_do_and_needs_neither_a_key_nor_a_container(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    for prompt in ("alpha", "beta"):
        harness.script(prompt, BUILDS_ITS_OWN, BUILDS_ITS_OWN)
    harness.run()
    sandboxes = list(harness.sandboxes)

    def forbidden(*args: object) -> None:
        pytest.fail("nothing was left to run, so no agent should be made")

    monkeypatch.setattr(cli, "ClaudeCodeAgent", forbidden)
    again = harness.run()

    assert again.exit_code == 0, again.output
    assert again.output.splitlines() == ["demo: 0 run(s) to start, up to 1 at a time."]
    assert harness.sandboxes == sandboxes
    assert db("SELECT COUNT(*) FROM runs") == [(4,)]


def test_a_done_run_is_not_run_again_when_only_some_are_left(harness: Harness) -> None:
    harness.script("alpha", BUILDS_ITS_OWN)
    harness.script("beta", BUILDS_ITS_OWN)
    write_study(repeats=1)
    harness.run()
    write_study(repeats=2)
    harness.script("alpha", BUILDS_ITS_OWN)
    harness.script("beta", BUILDS_ITS_OWN)

    outcome = harness.run()

    assert outcome.output.splitlines()[0] == "demo: 2 run(s) to start, up to 1 at a time."
    assert harness.agent.started == ["alpha", "beta", "alpha", "beta"]
    assert db("SELECT COUNT(*) FROM runs") == [(4,)]


def test_a_failed_run_is_skipped_until_retry_failed_is_passed(harness: Harness) -> None:
    harness.script("alpha", CRASHES, PICKS_CLERK, PICKS_CLERK)
    harness.script("beta", BUILDS_ITS_OWN, BUILDS_ITS_OWN)

    first = harness.run()
    second = harness.run()

    assert first.exit_code == 1
    assert "task 0 repeat 0 claude_code: undetermined (run_failed), cost $0.40, total $0.40" in (
        first.output
    )
    assert "Ran 4 attempt(s): 3 done, 1 failed." in first.output
    assert second.exit_code == 0, second.output
    assert second.output.splitlines() == [
        "demo: 0 run(s) to start, up to 1 at a time.",
        "Skipping 1 failed run(s); pass --retry-failed to run them again.",
    ]
    assert db("SELECT COUNT(*) FROM runs") == [(4,)]

    third = harness.run("--retry-failed")

    assert third.exit_code == 0, third.output
    assert third.output.splitlines() == [
        "demo: 1 run(s) to start, up to 1 at a time.",
        "task 0 repeat 0 claude_code: picked (Clerk), cost $0.20, total $1.20",
        "Ran 1 attempt(s): 1 done, 0 failed.",
    ]
    assert db(f"SELECT attempt, status FROM runs WHERE id = '{run_id(0, 0)}' ORDER BY attempt") == [
        (1, "failed"),
        (2, "done"),
    ]
    archived = Path("runs") / run_id(0, 0) / "attempts" / "1" / "transcript.jsonl"
    assert archived.is_file()


def test_a_run_that_ran_out_of_turns_is_told_apart_from_one_that_crashed(harness: Harness) -> None:
    harness.script("alpha", RUNS_OUT_OF_TURNS, BUILDS_ITS_OWN)
    harness.script("beta", BUILDS_ITS_OWN, BUILDS_ITS_OWN)

    outcome = harness.run()

    assert "task 0 repeat 0 claude_code: undetermined (max_turns), cost $0.90" in outcome.output
    assert db(f"SELECT verdict, reason FROM results WHERE run_id = '{run_id(0, 0)}'") == [
        ("undetermined", "max_turns")
    ]


def test_a_run_left_pending_by_a_refused_retry_is_started_not_replaced(harness: Harness) -> None:
    write_study(tasks=["alpha"], repeats=1)
    harness.script("alpha", BUILDS_ITS_OWN)
    with closing(Store(cli.DB_PATH)) as store:
        store.save_study("demo", "name: demo")
        store.next_attempt("demo", 0, "claude_code", 0)

    outcome = harness.run()

    assert outcome.exit_code == 0, outcome.output
    assert db("SELECT id, attempt, status FROM runs") == [(run_id(0, 0), 1, "done")]


def test_a_run_left_running_by_a_crash_counts_as_failed(harness: Harness) -> None:
    write_study(tasks=["alpha"], repeats=1)
    harness.script("alpha", BUILDS_ITS_OWN)
    with closing(Store(cli.DB_PATH)) as store:
        store.save_study("demo", "name: demo")
        crashed = store.next_attempt("demo", 0, "claude_code", 0)
        store.start_run(crashed, "test-model")
        store.begin_agent(crashed, "9.9.9")

    plain = harness.run()

    assert plain.output.splitlines() == [
        "demo: 0 run(s) to start, up to 1 at a time.",
        "Skipping 1 failed run(s); pass --retry-failed to run them again.",
    ]
    assert db("SELECT attempt, status FROM runs") == [(1, "running")]

    retried = harness.run("--retry-failed")

    assert retried.exit_code == 0, retried.output
    assert db("SELECT attempt, status FROM runs ORDER BY attempt") == [(1, "failed"), (2, "done")]
    assert db("SELECT attempt, kind FROM events WHERE kind = 'run_interrupted'") == [
        (1, "run_interrupted")
    ]


def test_no_run_starts_once_one_more_could_take_the_study_over_its_budget(
    harness: Harness,
) -> None:
    write_study(study_budget_usd=2.0)
    half_a_dollar = Attempt(cost=0.5)
    harness.script("alpha", half_a_dollar, half_a_dollar)
    harness.script("beta", half_a_dollar, half_a_dollar)

    outcome = harness.run()

    assert outcome.exit_code == 1
    assert outcome.output.splitlines() == [
        "demo: 4 run(s) to start, up to 1 at a time.",
        "task 0 repeat 0 claude_code: no_sdk, cost $0.50, total $0.50",
        "task 1 repeat 0 claude_code: no_sdk, cost $0.50, total $1.00",
        "task 0 repeat 1 claude_code: no_sdk, cost $0.50, total $1.50",
        "Ran 3 attempt(s): 3 done, 0 failed.",
        "Stopped with 1 run(s) not started: $1.50 of $2.00 spent on demo, "
        "and the next run could cost up to $1.00.",
    ]
    assert harness.agent.started == ["alpha", "beta", "alpha"]
    assert db("SELECT id FROM runs ORDER BY id") == [
        (run_id(0, 0),),
        (run_id(0, 1),),
        (run_id(1, 0),),
    ]


def test_a_study_with_no_room_for_one_run_starts_nothing(harness: Harness) -> None:
    write_study(study_budget_usd=0.5)

    outcome = harness.run()

    assert outcome.exit_code == 1
    assert outcome.output.splitlines() == [
        "demo: 4 run(s) to start, up to 1 at a time.",
        "Ran 0 attempt(s): 0 done, 0 failed.",
        "Stopped with 4 run(s) not started: $0.00 of $0.50 spent on demo, "
        "and the next run could cost up to $1.00.",
    ]
    assert harness.sandboxes == []
    assert db("SELECT COUNT(*) FROM runs") == [(0,)]


def test_runs_in_flight_count_at_the_full_run_budget(harness: Harness) -> None:
    write_study(tasks=["a", "b", "c", "d"], repeats=1, study_budget_usd=2.5)
    for prompt in "abcd":
        harness.script(prompt, BUILDS_ITS_OWN)
    harness.sandbox_delay = 0.2
    harness.hold_first(2)

    outcome = harness.run("--concurrency", "4")

    assert outcome.exit_code == 0, outcome.output
    assert max(harness.agent.running_rows) == 2
    assert len(harness.agent.started) == 4
    assert outcome.output.count("claude_code: no_sdk") == 4
    assert outcome.output.splitlines()[0] == "demo: 4 run(s) to start, up to 4 at a time."


def test_runs_go_on_in_parallel_when_the_budget_allows(harness: Harness) -> None:
    write_study(tasks=["a", "b", "c"], repeats=1)
    for prompt in "abc":
        harness.script(prompt, BUILDS_ITS_OWN)
    harness.hold_first(3, seconds=0.0)

    outcome = harness.run("--concurrency", "3")

    assert outcome.exit_code == 0, outcome.output
    assert max(harness.agent.running_rows) == 3
    assert db("SELECT COUNT(*) FROM runs WHERE status = 'done'") == [(3,)]


def test_no_more_runs_than_asked_for_go_at_once(harness: Harness) -> None:
    write_study(tasks=["a", "b", "c", "d"], repeats=1)
    for prompt in "abcd":
        harness.script(prompt, BUILDS_ITS_OWN)
    harness.hold_first(2)

    outcome = harness.run("--concurrency", "2")

    assert outcome.exit_code == 0, outcome.output
    assert max(harness.agent.running_rows) == 2
    assert db("SELECT COUNT(*) FROM runs WHERE status = 'done'") == [(4,)]


def test_the_total_on_each_line_adds_what_the_agents_reported_not_what_is_held_back(
    harness: Harness,
) -> None:
    write_study(tasks=["a", "b"], repeats=1)
    harness.script("a", Attempt(cost=0.25))
    harness.script("b", Attempt(cost=0.5))
    barrier = threading.Barrier(2, timeout=10)

    def meet_then_let_the_cheaper_one_finish_first(box: Sandbox, number: int) -> None:
        barrier.wait()
        if "-t1-" in box.name:
            time.sleep(0.6)

    harness.agent.on_start = meet_then_let_the_cheaper_one_finish_first

    outcome = harness.run("--concurrency", "2")

    lines = [line for line in outcome.output.splitlines() if "total" in line]
    assert [line.rsplit("total ", 1)[1] for line in lines] == ["$0.25", "$0.75"]


def test_the_total_grows_by_each_line_even_when_runs_finish_together(harness: Harness) -> None:
    write_study(tasks=["a", "b"], repeats=1)
    harness.script("a", Attempt(cost=0.25))
    harness.script("b", Attempt(cost=0.5))
    harness.hold_first(2, seconds=0.0)
    outcome = harness.run("--concurrency", "2")
    lines = [line for line in outcome.output.splitlines() if "total" in line]
    costs = [float(line.split("cost $")[1].split(",")[0]) for line in lines]
    totals = [float(line.rsplit("total $", 1)[1]) for line in lines]

    assert sorted(costs) == [0.25, 0.5]
    assert totals == [costs[0], 0.75]


def test_a_run_that_reported_no_cost_is_not_in_the_total(harness: Harness) -> None:
    write_study(tasks=["a"], repeats=1)
    harness.script("a", Attempt(exit_code=124, cost=None), Attempt(cost=0.25))

    first = harness.run()
    second = harness.run("--retry-failed")

    assert "task 0 repeat 0 claude_code: undetermined (run_failed), cost unknown, total $0.00" in (
        first.output
    )
    assert "task 0 repeat 0 claude_code: no_sdk, cost $0.25, total $0.25" in second.output


def test_concurrency_must_be_at_least_one(harness: Harness) -> None:
    outcome = harness.run("--concurrency", "0")

    assert outcome.exit_code == 2
    assert harness.sandboxes == []


def test_a_sandbox_that_cannot_start_fails_that_run_and_the_others_go_on(harness: Harness) -> None:
    harness.broken.add(f"whichsdk-{run_id(0, 0)}")
    harness.script("alpha", BUILDS_ITS_OWN)
    harness.script("beta", BUILDS_ITS_OWN, BUILDS_ITS_OWN)

    outcome = harness.run()

    assert outcome.exit_code == 1
    assert (
        "task 0 repeat 0 claude_code: failed (docker exited 125: no space left on device), "
        "cost unknown, total $0.00"
    ) in outcome.output
    assert "Ran 4 attempt(s): 3 done, 1 failed." in outcome.output
    assert db(f"SELECT status, cost_usd FROM runs WHERE id = '{run_id(0, 0)}'") == [
        ("failed", None)
    ]
    assert db(f"SELECT kind FROM events WHERE run_id = '{run_id(0, 0)}' ORDER BY rowid") == [
        ("run_start",),
        ("run_error",),
    ]
    assert db(f"SELECT COUNT(*) FROM results WHERE run_id = '{run_id(0, 0)}'") == [(0,)]


def test_the_key_is_needed_only_when_something_will_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(*args: object) -> None:
        raise ValueError("ANTHROPIC_API_KEY is not set in .env")

    monkeypatch.setattr(cli, "ClaudeCodeAgent", no_key)

    outcome = harness.run()

    assert outcome.exit_code == 1
    assert "Cannot read the API key" in outcome.output
    assert db("SELECT COUNT(*) FROM runs") == [(0,)]


def test_a_missing_starter_template_stops_before_a_database_is_made(harness: Harness) -> None:
    write_study(starter_template="nowhere")

    outcome = harness.run()

    assert outcome.exit_code == 1
    assert "Starter template nowhere is not a directory." in outcome.output
    assert not cli.DB_PATH.exists()


def test_ctrl_c_fails_the_runs_in_flight_removes_their_containers_and_the_next_run_goes_on(
    harness: Harness,
) -> None:
    write_study(tasks=["a", "b", "c", "d"], repeats=1)
    for prompt in "abcd":
        harness.script(prompt, PICKS_CLERK, BUILDS_ITS_OWN)
    barrier = threading.Barrier(2, timeout=10)

    def press_ctrl_c_and_wait_to_be_removed(box: Sandbox, number: int) -> None:
        if number > 2:
            return
        barrier.wait()
        if number == 1:
            time.sleep(0.05)
            _thread.interrupt_main()
        assert harness.released[box.name].wait(timeout=10)

    harness.agent.on_start = press_ctrl_c_and_wait_to_be_removed

    interrupted = harness.run("--concurrency", "2")

    assert interrupted.exit_code == 130
    assert "Interrupted: the runs in flight are marked failed, their containers removed." in (
        interrupted.output
    )
    assert set(harness.removed) == {f"whichsdk-{run_id(0, 0)}", f"whichsdk-{run_id(1, 0)}"}
    assert db("SELECT id, status FROM runs ORDER BY id") == [
        (run_id(0, 0), "failed"),
        (run_id(1, 0), "failed"),
    ]
    assert db("SELECT run_id FROM events WHERE kind = 'run_interrupted' ORDER BY run_id") == [
        (run_id(0, 0),),
        (run_id(1, 0),),
    ]
    assert db("SELECT run_id, verdict, reason FROM results ORDER BY run_id") == [
        (run_id(0, 0), "undetermined", "run_failed"),
        (run_id(1, 0), "undetermined", "run_failed"),
    ]
    assert db("SELECT COUNT(*) FROM runs WHERE cost_usd = 0.2") == [(2,)]

    harness.removed.clear()
    carried_on = harness.run("--concurrency", "2")

    lines = carried_on.output.splitlines()
    assert carried_on.exit_code == 0, carried_on.output
    assert lines[:2] == [
        "demo: 2 run(s) to start, up to 2 at a time.",
        "Skipping 2 failed run(s); pass --retry-failed to run them again.",
    ]
    assert sorted(line.split(":")[0] for line in lines[2:-1]) == [
        "task 2 repeat 0 claude_code",
        "task 3 repeat 0 claude_code",
    ]
    assert lines[-1] == "Ran 2 attempt(s): 2 done, 0 failed."

    again = harness.run("--retry-failed")

    assert again.exit_code == 0, again.output
    assert db("SELECT id, attempt, status FROM runs ORDER BY id, attempt") == [
        (run_id(0, 0), 1, "failed"),
        (run_id(0, 0), 2, "done"),
        (run_id(1, 0), 1, "failed"),
        (run_id(1, 0), 2, "done"),
        (run_id(2, 0), 1, "done"),
        (run_id(3, 0), 1, "done"),
    ]
    assert harness.removed == []


def test_a_container_made_after_ctrl_c_is_still_removed(harness: Harness) -> None:
    write_study(tasks=["a"], repeats=1)
    harness.script("a", PICKS_CLERK)
    harness.sandbox_delay = 0.2

    def press_ctrl_c_while_the_container_is_starting(name: str) -> None:
        time.sleep(0.05)  
        _thread.interrupt_main()

    harness.on_sandbox = press_ctrl_c_while_the_container_is_starting
    harness.agent.on_start = lambda box, number: harness.released[box.name].wait(timeout=10)

    outcome = harness.run()

    assert outcome.exit_code == 130
    assert harness.removed.count(f"whichsdk-{run_id(0, 0)}") > 1
    assert db("SELECT id, status FROM runs") == [(run_id(0, 0), "failed")]


def test_a_run_that_breaks_stops_the_others_that_are_in_flight(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_study(tasks=["a", "b"], repeats=1)
    harness.script("a", BUILDS_ITS_OWN)
    harness.script("b", BUILDS_ITS_OWN)
    real_detect = cli._detect_attempt

    def detect_that_breaks_for_the_first(
        spec: StudySpec, attempt_dir: Path, finished: bool
    ) -> Detection:
        if "-t0-" in attempt_dir.name:
            raise RuntimeError("a detector bug")
        return real_detect(spec, attempt_dir, finished)

    def wait_to_be_removed(box: Sandbox, number: int) -> None:
        if "-t1-" in box.name:
            assert harness.released[box.name].wait(timeout=10)

    monkeypatch.setattr(cli, "_detect_attempt", detect_that_breaks_for_the_first)
    harness.agent.on_start = wait_to_be_removed

    outcome = harness.run("--concurrency", "2")

    assert isinstance(outcome.exception, RuntimeError)
    assert set(harness.removed) == {f"whichsdk-{run_id(1, 0)}"}
    assert db("SELECT id, status FROM runs ORDER BY id") == [
        (run_id(0, 0), "done"),
        (run_id(1, 0), "failed"),
    ]
    assert db("SELECT COUNT(*) FROM results WHERE run_id = '" + run_id(0, 0) + "'") == [(0,)]


def test_status_after_a_run_shows_what_it_did(harness: Harness) -> None:
    harness.script("alpha", PICKS_CLERK, CRASHES)
    harness.script("beta", BUILDS_ITS_OWN, BUILDS_ITS_OWN)
    harness.run()

    outcome = harness.status()

    assert outcome.exit_code == 0, outcome.output
    assert outcome.output.splitlines()[:4] == [
        "demo: 4 runs (2 tasks x 2 repeats x 1 agent(s))",
        "done 3, failed 1, running 0, pending 0",
        "$1.00 of $20.00 spent on demo",
        "Verdicts of the newest attempt of each run:",
    ]
    assert f"  {'picked':<16}  1" in outcome.output
    assert f"  {'no_sdk':<16}  2" in outcome.output
    assert f"  {'undetermined':<16}  1" in outcome.output
