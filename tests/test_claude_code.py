from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from whichsdk.agents.base import AgentResult
from whichsdk.agents.claude_code import API_KEY_VARIABLE, ClaudeCodeAgent
from whichsdk.sandbox import ExecResult, Sandbox

KEY = "sk-ant-api03-test-key"
INIT = '{"type":"system","subtype":"init"}'
ASSISTANT = '{"type":"assistant","message":{}}'
RESULT = '{"type":"result","subtype":"success","total_cost_usd":1.25}'


class FakeSandbox(Sandbox):
    def __init__(self, output: str, exit_code: int) -> None:
        super().__init__("fake")
        self._result = ExecResult(exit_code, output)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def exec(
        self, args: Sequence[str], env: Mapping[str, str] | None = None, check: bool = True
    ) -> ExecResult:
        self.calls.append((list(args), dict(env or {})))
        return self._result


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(f"{API_KEY_VARIABLE}={KEY}\n", encoding="utf-8")
    return path


def run_agent(
    env_file: Path, lines: list[str], exit_code: int = 0
) -> tuple[AgentResult, FakeSandbox]:
    box = FakeSandbox("\n".join(lines) + "\n", exit_code)
    result = ClaudeCodeAgent(env_file, "test-model", 2.0).run(box, "add login", 600, 40)
    return result, box


def test_reads_the_cost_from_the_final_result_event(env_file: Path) -> None:
    result, _ = run_agent(env_file, [INIT, ASSISTANT, RESULT])

    assert result.cost_usd == 1.25
    assert result.exit_code == 0
    assert result.transcript_lines == [INIT, ASSISTANT, RESULT]


def test_the_last_result_event_wins(env_file: Path) -> None:
    earlier = '{"type":"result","total_cost_usd":0.1}'

    result, _ = run_agent(env_file, [INIT, earlier, ASSISTANT, RESULT])

    assert result.cost_usd == 1.25


def test_cost_is_unknown_without_a_result_event(env_file: Path) -> None:
    result, _ = run_agent(env_file, [INIT, ASSISTANT], exit_code=124)

    assert result.cost_usd is None
    assert result.exit_code == 124


def test_plain_text_output_is_kept_but_ignored_for_the_cost(env_file: Path) -> None:
    result, _ = run_agent(env_file, [INIT, RESULT, "timeout: sending signal TERM"])

    assert result.cost_usd == 1.25
    assert result.transcript_lines[-1] == "timeout: sending signal TERM"


def test_the_api_key_is_redacted_from_the_transcript(env_file: Path) -> None:
    leaky = f'{{"type":"user","content":"ANTHROPIC_API_KEY={KEY}"}}'

    result, _ = run_agent(env_file, [INIT, leaky, RESULT])

    assert not any(KEY in line for line in result.transcript_lines)
    assert "ANTHROPIC_API_KEY=[redacted]" in result.transcript_lines[1]


def test_the_key_travels_in_the_environment_and_not_the_command(env_file: Path) -> None:
    _, box = run_agent(env_file, [RESULT])

    [(args, env)] = box.calls
    assert args[:4] == ["timeout", "--kill-after=30", "600", "claude"]
    assert args[args.index("-p") + 1] == "add login"
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--max-turns") + 1] == "40"
    assert {"--verbose", "--dangerously-skip-permissions"} <= set(args)
    assert env == {API_KEY_VARIABLE: KEY}
    assert not any(KEY in arg for arg in args)


def test_the_model_and_run_budget_are_passed_to_claude(env_file: Path) -> None:
    _, box = run_agent(env_file, [RESULT])

    [(args, _)] = box.calls
    assert args[args.index("--model") + 1] == "test-model"
    assert args[args.index("--max-budget-usd") + 1] == "2.0"


def test_the_version_is_read_from_the_installed_claude(env_file: Path) -> None:
    box = FakeSandbox("2.1.310 (Claude Code)\n", 0)

    version = ClaudeCodeAgent(env_file, "test-model", 2.0).version(box)

    assert version == "2.1.310"
    assert box.calls == [(["claude", "--version"], {})]


@pytest.mark.parametrize(
    "content",
    [
        b"ANTHROPIC_API_KEY=abc\r\n",
        b'ANTHROPIC_API_KEY="abc"\n',
        b"\xef\xbb\xbfANTHROPIC_API_KEY=abc\n",
        b"# a comment\nOTHER=x\nANTHROPIC_API_KEY=abc\n",
    ],
)
def test_reads_the_key_from_common_env_file_shapes(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / ".env"
    path.write_bytes(content)

    box = FakeSandbox(RESULT, 0)
    ClaudeCodeAgent(path, "test-model", 1.0).run(box, "p", 60, 5)

    assert box.calls[0][1] == {API_KEY_VARIABLE: "abc"}


@pytest.mark.parametrize("content", [b"OTHER=x\n", b"ANTHROPIC_API_KEY=\n", b""])
def test_a_missing_or_empty_key_is_an_error(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / ".env"
    path.write_bytes(content)

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is not set"):
        ClaudeCodeAgent(path, "test-model", 1.0)


def test_a_missing_env_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ClaudeCodeAgent(tmp_path / ".env", "test-model", 1.0)
