import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from whichsdk import cli
from whichsdk.agents.base import Agent, AgentResult
from whichsdk.sandbox import Sandbox, SandboxError
from whichsdk.store import Store

RUN_ID = "demo-t0-claude_code-r0"

STUDY: dict[str, object] = {
    "name": "demo",
    "category": "auth",
    "competitors": [{"name": "Clerk", "npm_packages": ["@clerk/nextjs"]}],
    "starter_template": "starter",
    "tasks": ["add login"],
    "agents": [{"name": "claude_code", "model": "test-model"}],
    "repeats": 3,
    "timeout_seconds": 600,
    "max_turns": 10,
    "run_budget_usd": 1.0,
    "study_budget_usd": 3.0,
}


class FakeSandbox(Sandbox):
    def __init__(self, name: str, marker: str) -> None:
        super().__init__(name)
        self._marker = marker

    def copy_out(self, names: Iterable[str], directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "package.json").write_text(self._marker, encoding="utf-8")


class UncopyableSandbox(FakeSandbox):
    def copy_out(self, names: Iterable[str], directory: Path) -> None:
        raise SandboxError("docker exited 1: it broke")


class FakeAgent(Agent):
    def __init__(self, results: list[AgentResult], runs: list[tuple[str, int, int]]) -> None:
        self._results = results
        self._runs = runs

    def version(self, container: Sandbox) -> str:
        return "9.9.9"

    def run(self, container: Sandbox, prompt: str, timeout: int, max_turns: int) -> AgentResult:
        if not self._results:
            pytest.fail("the agent ran when it should not have")
        self._runs.append((prompt, timeout, max_turns))
        return self._results.pop(0)


class Harness:
    def __init__(self) -> None:
        self.results: list[AgentResult] = []
        self.runs: list[tuple[str, int, int]] = []
        self.agents_built: list[tuple[str, float]] = []
        self.sandboxes: list[str] = []

    def make_agent(self, env_file: Path, model: str, run_budget_usd: float) -> Agent:
        self.agents_built.append((model, run_budget_usd))
        return FakeAgent(self.results, self.runs)

    @contextmanager
    def start_sandbox(self, name: str, starter: Path) -> Iterator[Sandbox]:
        self.sandboxes.append(name)
        yield FakeSandbox(name, f"sandbox {len(self.sandboxes)}")

    def run_one(self, *args: str) -> Result:
        return CliRunner().invoke(cli.app, ["run-one", "study.yaml", "--task", "0", *args])


def write_study(**overrides: object) -> None:
    Path("study.yaml").write_text(yaml.safe_dump({**STUDY, **overrides}), encoding="utf-8")


def agent_result(exit_code: int, cost: float | None, transcript: str) -> AgentResult:
    return AgentResult(exit_code, [transcript], cost, 12.0)


def db(sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(cli.DB_PATH)) as connection:
        return connection.execute(sql).fetchall()


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.chdir(tmp_path)
    Path("starter").mkdir()
    write_study()
    harness = Harness()
    monkeypatch.setattr(cli, "ClaudeCodeAgent", harness.make_agent)
    monkeypatch.setattr(cli, "start_sandbox", harness.start_sandbox)
    return harness


def test_a_retry_moves_the_earlier_attempt_aside_and_keeps_both_costs(harness: Harness) -> None:
    harness.results += [agent_result(1, 0.4, "first"), agent_result(0, 0.3, "second")]

    first = harness.run_one()
    second = harness.run_one()

    assert (first.exit_code, second.exit_code) == (1, 0), second.output
    run_dir = Path("runs") / RUN_ID
    assert (run_dir / "transcript.jsonl").read_text(encoding="utf-8") == "second\n"
    assert (run_dir / "package.json").read_text(encoding="utf-8") == "sandbox 2"
    archived = run_dir / "attempts" / "1"
    assert (archived / "transcript.jsonl").read_text(encoding="utf-8") == "first\n"
    assert (archived / "package.json").read_text(encoding="utf-8") == "sandbox 1"
    assert (archived / "baseline" / "package.json").read_text(encoding="utf-8") == "sandbox 1"
    assert db("SELECT attempt, status, cost_usd, transcript_path FROM runs ORDER BY attempt") == [
        (1, "failed", 0.4, f"runs/{RUN_ID}/attempts/1/transcript.jsonl"),
        (2, "done", 0.3, f"runs/{RUN_ID}/transcript.jsonl"),
    ]


def test_a_third_attempt_is_archived_next_to_the_first(harness: Harness) -> None:
    harness.results += [
        agent_result(1, 0.1, "one"),
        agent_result(1, 0.1, "two"),
        agent_result(0, 0.1, "three"),
    ]

    for _ in range(3):
        harness.run_one()

    run_dir = Path("runs") / RUN_ID
    assert sorted(path.name for path in (run_dir / "attempts").iterdir()) == ["1", "2"]
    assert (run_dir / "attempts" / "1" / "transcript.jsonl").read_text(encoding="utf-8") == "one\n"
    assert (run_dir / "attempts" / "2" / "transcript.jsonl").read_text(encoding="utf-8") == "two\n"
    assert (run_dir / "transcript.jsonl").read_text(encoding="utf-8") == "three\n"
    assert db("SELECT attempt, status FROM runs ORDER BY attempt") == [
        (1, "failed"),
        (2, "failed"),
        (3, "done"),
    ]


def test_an_attempt_left_running_is_kept_and_the_run_starts_again(harness: Harness) -> None:
    with closing(Store(cli.DB_PATH)) as store:
        store.save_study("demo", "name: demo")
        crashed = store.next_attempt("demo", 0, "claude_code", 0)
        store.start_run(crashed, "test-model")
        store.begin_agent(crashed, "9.9.9")
    partial = Path("runs") / RUN_ID / "baseline"
    partial.mkdir(parents=True)
    (partial / "package.json").write_text("partial", encoding="utf-8")
    harness.results.append(agent_result(0, 0.2, "after the crash"))

    outcome = harness.run_one()

    assert outcome.exit_code == 0, outcome.output
    assert db("SELECT attempt, status, cost_usd FROM runs ORDER BY attempt") == [
        (1, "failed", None),
        (2, "done", 0.2),
    ]
    kept = Path("runs") / RUN_ID / "attempts" / "1" / "baseline" / "package.json"
    assert kept.read_text(encoding="utf-8") == "partial"
    assert "1 attempt(s) with no reported cost counted at $1.00 each" in outcome.output


def test_a_done_run_is_not_run_again(harness: Harness) -> None:
    harness.results.append(agent_result(0, 0.2, "ok"))
    harness.run_one()

    again = harness.run_one()

    assert again.exit_code == 1
    assert "already done" in again.output
    assert len(harness.sandboxes) == 1
    assert db("SELECT COUNT(*) FROM runs") == [(1,)]


def test_a_refused_retry_changes_nothing_and_never_starts_a_sandbox(harness: Harness) -> None:
    write_study(study_budget_usd=1.5)
    harness.results.append(agent_result(1, 0.8, "first"))
    harness.run_one()

    refused = harness.run_one()

    assert refused.exit_code == 1
    assert "$0.80 of $1.50 spent on demo" in refused.output
    run_dir = Path("runs") / RUN_ID
    assert (run_dir / "transcript.jsonl").read_text(encoding="utf-8") == "first\n"
    assert not (run_dir / "attempts").exists()
    assert len(harness.sandboxes) == 1
    assert db("SELECT attempt, status FROM runs ORDER BY attempt") == [
        (1, "failed"),
        (2, "pending"),
    ]


def test_a_run_that_exactly_reaches_the_study_budget_is_allowed(harness: Harness) -> None:
    write_study(study_budget_usd=1.5)
    harness.results += [agent_result(0, 0.5, "first"), agent_result(0, 0.1, "second")]
    harness.run_one()

    second = harness.run_one("--repeat", "1")

    assert second.exit_code == 0, second.output
    assert "$0.50 of $1.50 spent on demo; this run is capped at $1.00." in second.output


def test_an_attempt_with_no_reported_cost_counts_at_the_run_budget(harness: Harness) -> None:
    write_study(study_budget_usd=1.5)
    harness.results.append(agent_result(124, None, "timed out"))
    harness.run_one()

    refused = harness.run_one("--repeat", "1")

    assert refused.exit_code == 1
    assert (
        "$1.00 of $1.50 spent on demo "
        "(1 attempt(s) with no reported cost counted at $1.00 each)" in refused.output
    )
    assert db("SELECT cost_usd FROM runs WHERE repeat = 0") == [(None,)]


@pytest.mark.parametrize("breaks_at", ["start", "baseline copy"])
def test_a_sandbox_that_fails_before_the_agent_costs_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, breaks_at: str
) -> None:
    @contextmanager
    def broken(name: str, starter: Path) -> Iterator[Sandbox]:
        if breaks_at == "start":
            raise SandboxError("docker exited 125: it broke")
        yield UncopyableSandbox(name, "never written")

    monkeypatch.setattr(cli, "start_sandbox", broken)
    outcome = harness.run_one()

    assert outcome.exit_code == 1
    assert "it broke" in outcome.output
    assert db(
        "SELECT status, model, claude_code_version, agent_started_at, cost_usd FROM runs"
    ) == [("failed", "test-model", None, None, None)]
    assert db("SELECT kind FROM events ORDER BY rowid") == [("run_start",), ("run_error",)]

    monkeypatch.setattr(cli, "start_sandbox", harness.start_sandbox)
    harness.results.append(agent_result(0, 0.1, "ok"))
    retry = harness.run_one()

    assert retry.exit_code == 0, retry.output
    assert "$0.00 of $3.00 spent on demo; this run is capped at $1.00." in retry.output


def test_the_agent_is_configured_from_the_spec_and_the_model_and_version_are_stored(
    harness: Harness,
) -> None:
    agents = [{"name": "claude_code", "model": "claude-opus-5-5"}]
    write_study(agents=agents, run_budget_usd=0.75, timeout_seconds=90, max_turns=7)
    harness.results.append(agent_result(0, 0.1, "ok"))

    outcome = harness.run_one()

    assert outcome.exit_code == 0, outcome.output
    assert harness.agents_built == [("claude-opus-5-5", 0.75)]
    assert harness.runs == [("add login", 90, 7)]
    assert db("SELECT model, claude_code_version FROM runs") == [("claude-opus-5-5", "9.9.9")]
    [(payload,)] = db("SELECT payload FROM events WHERE kind = 'run_start'")
    assert json.loads(str(payload))["model"] == "claude-opus-5-5"
