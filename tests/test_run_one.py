import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from whichsdk import cli
from whichsdk.agents.base import Agent, AgentResult
from whichsdk.sandbox import ExecResult, Sandbox, SandboxError
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
    def __init__(
        self,
        name: str,
        marker: str,
        build_exit_code: int = 0,
        calls: list[tuple[str, ...]] | None = None,
    ) -> None:
        super().__init__(name)
        self._marker = marker
        self._build_exit_code = build_exit_code
        self._calls = [] if calls is None else calls

    def exec(
        self, args: Sequence[str], env: Mapping[str, str] | None = None, check: bool = True
    ) -> ExecResult:
        self._calls.append(("build",))
        return ExecResult(self._build_exit_code, f"built {self._marker}\n")

    def copy_out(self, names: Iterable[str], directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "package.json").write_text(self._marker, encoding="utf-8")

    def changes_since_baseline(self, exclude: Iterable[str]) -> str:
        self._calls.append(("diff", *exclude))
        return f"diff of {self._marker}\n"


class UncopyableSandbox(FakeSandbox):
    def copy_out(self, names: Iterable[str], directory: Path) -> None:
        raise SandboxError("docker exited 1: it broke")


class DetectingSandbox(FakeSandbox):
    def copy_out(self, names: Iterable[str], directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        dependencies = {"next": "16.4.3"}
        if directory.name != "baseline":
            dependencies["better-auth"] = "^1.3.4"
        package = json.dumps({"dependencies": dependencies})
        (directory / "package.json").write_text(package, encoding="utf-8")

    def changes_since_baseline(self, exclude: Iterable[str]) -> str:
        return '+++ b/lib/auth.ts\n+import { betterAuth } from "better-auth";\n'


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
        self.calls: list[tuple[str, ...]] = []
        self.build_exit_code = 0

    def make_agent(self, env_file: Path, model: str, run_budget_usd: float) -> Agent:
        self.agents_built.append((model, run_budget_usd))
        return FakeAgent(self.results, self.runs)

    @contextmanager
    def start_sandbox(self, name: str, starter: Path) -> Iterator[Sandbox]:
        self.sandboxes.append(name)
        marker = f"sandbox {len(self.sandboxes)}"
        yield FakeSandbox(name, marker, self.build_exit_code, self.calls)

    def run_one(self, *args: str) -> Result:
        return CliRunner().invoke(cli.app, ["run-one", "study.yaml", "--task", "0", *args])


def write_study(**overrides: object) -> None:
    Path("study.yaml").write_text(yaml.safe_dump({**STUDY, **overrides}), encoding="utf-8")


def agent_result(exit_code: int, cost: float | None, transcript: str) -> AgentResult:
    return AgentResult(exit_code, [transcript], cost, 12.0)


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
    assert (run_dir / "changes.diff").read_text(encoding="utf-8") == "diff of sandbox 2\n"
    assert (archived / "changes.diff").read_text(encoding="utf-8") == "diff of sandbox 1\n"
    assert (archived / "build.log").read_text(encoding="utf-8") == "built sandbox 1\n"
    assert db("SELECT attempt, status, cost_usd, transcript_path FROM runs ORDER BY attempt") == [
        (1, "failed", 0.4, f"runs/{RUN_ID}/attempts/1/transcript.jsonl"),
        (2, "done", 0.3, f"runs/{RUN_ID}/transcript.jsonl"),
    ]
    assert db("SELECT attempt, build_log_path FROM results ORDER BY attempt") == [
        (1, f"runs/{RUN_ID}/attempts/1/build.log"),
        (2, f"runs/{RUN_ID}/build.log"),
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


def test_the_changes_and_the_build_log_are_saved_next_to_the_package_files(
    harness: Harness,
) -> None:
    harness.results.append(agent_result(0, 0.2, "ok"))

    outcome = harness.run_one()

    assert outcome.exit_code == 0, outcome.output
    run_dir = Path("runs") / RUN_ID
    assert (run_dir / "changes.diff").read_text(encoding="utf-8") == "diff of sandbox 1\n"
    assert (run_dir / "build.log").read_text(encoding="utf-8") == "built sandbox 1\n"
    assert db("SELECT payload FROM events WHERE kind = 'build_finished'") == [('{"passed": true}',)]
    assert db("SELECT build_passed, build_log_path FROM results") == [
        (1, f"runs/{RUN_ID}/build.log")
    ]
    assert "Verdict: undetermined; build passed" in outcome.output


def test_the_diff_is_taken_before_the_build_can_change_any_files(harness: Harness) -> None:
    harness.results.append(agent_result(0, 0.2, "ok"))

    harness.run_one()

    assert [call[0] for call in harness.calls] == ["diff", "build"]


def test_the_diff_leaves_the_lockfiles_out(harness: Harness) -> None:
    harness.results.append(agent_result(0, 0.2, "ok"))

    harness.run_one()

    assert harness.calls[0] == ("diff", *cli.LOCKFILES)
    assert "package.json" not in harness.calls[0]


def test_a_failing_build_is_recorded_and_does_not_fail_the_run(harness: Harness) -> None:
    harness.build_exit_code = 1
    harness.results.append(agent_result(0, 0.2, "ok"))

    outcome = harness.run_one()

    assert outcome.exit_code == 0, outcome.output
    assert db("SELECT status FROM runs") == [("done",)]
    assert db("SELECT payload FROM events WHERE kind = 'build_finished'") == [
        ('{"passed": false}',)
    ]
    assert db("SELECT build_passed FROM results") == [(0,)]
    assert (Path("runs") / RUN_ID / "build.log").is_file()
    assert "build failed" in outcome.output


def test_the_verdict_is_decided_from_what_the_run_saved(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    competitors = [
        {"name": "Auth.js", "npm_packages": ["next-auth"]},
        {"name": "Better Auth", "npm_packages": ["better-auth"]},
    ]
    write_study(competitors=competitors)

    @contextmanager
    def detecting(name: str, starter: Path) -> Iterator[Sandbox]:
        yield DetectingSandbox(name, "sandbox")

    monkeypatch.setattr(cli, "start_sandbox", detecting)
    transcript = [
        *bash("a", "npm i next-auth"),
        *bash("b", "npm rm next-auth"),
        *bash("c", "npm i better-auth"),
    ]
    harness.results.append(AgentResult(0, transcript, 0.2, 12.0))

    outcome = harness.run_one()

    assert outcome.exit_code == 0, outcome.output
    assert "Verdict: picked (Better Auth); build passed" in outcome.output
    assert db(
        "SELECT verdict, picked_competitor, added, imported, switches, searched FROM results"
    ) == [
        (
            "picked",
            "Better Auth",
            '{"better-auth": "Better Auth"}',
            '{"better-auth": "Better Auth"}',
            '[{"from_competitor": "Auth.js", "to_competitor": "Better Auth"}]',
            0,
        )
    ]


def test_a_detector_that_breaks_never_loses_the_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(spec: object, attempt_dir: Path) -> None:
        raise RuntimeError("a detector bug")

    monkeypatch.setattr(cli, "_detect_attempt", broken)
    harness.results.append(agent_result(0, 0.2, "ok"))

    outcome = harness.run_one()

    assert isinstance(outcome.exception, RuntimeError)
    assert db("SELECT status, cost_usd FROM runs") == [("done", 0.2)]
    assert db("SELECT COUNT(*) FROM results") == [(0,)]
    assert (Path("runs") / RUN_ID / "transcript.jsonl").is_file()


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
