import json
import time
from pathlib import Path

from whichsdk.agents.base import Agent, AgentResult
from whichsdk.sandbox import Sandbox

API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
KILL_GRACE_SECONDS = 30


class ClaudeCodeAgent(Agent):
    """Claude Code CLI in print mode, with its stream-json output kept as the transcript."""

    def __init__(self, env_file: Path, model: str, run_budget_usd: float) -> None:
        self._api_key = _read_api_key(env_file)
        self._model = model
        self._run_budget_usd = run_budget_usd

    def version(self, container: Sandbox) -> str:
        """Return the Claude Code version installed in the container."""
        return container.exec(["claude", "--version"]).output.split()[0]

    def run(self, container: Sandbox, prompt: str, timeout: int, max_turns: int) -> AgentResult:
        """Run claude -p in the container and read the cost from the final result event."""
        command = [
            "timeout",
            f"--kill-after={KILL_GRACE_SECONDS}",
            str(timeout),
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--model",
            self._model,
            "--max-turns",
            str(max_turns),
            "--max-budget-usd",
            str(self._run_budget_usd),
        ]
        started = time.monotonic()
        result = container.exec(command, env={API_KEY_VARIABLE: self._api_key}, check=False)
        duration = round(time.monotonic() - started, 1)
        output = result.output.replace(self._api_key, "[redacted]")
        lines = [line for line in output.split("\n") if line]
        return AgentResult(result.exit_code, lines, _read_cost(lines), duration)


def _read_api_key(env_file: Path) -> str:
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        name, _, value = line.partition("=")
        value = value.strip().strip("\"'")
        if name.strip() == API_KEY_VARIABLE and value:
            return value
    raise ValueError(f"{API_KEY_VARIABLE} is not set in {env_file}")


def _read_cost(lines: list[str]) -> float | None:
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            return event.get("total_cost_usd")
    return None
