from abc import ABC, abstractmethod
from dataclasses import dataclass

from whichsdk.sandbox import Sandbox


@dataclass(frozen=True)
class AgentResult:
    """What one agent run produced; cost_usd is None when the agent never reported it."""

    exit_code: int
    transcript_lines: list[str]
    cost_usd: float | None
    duration_s: float


class Agent(ABC):
    """A coding agent that can be run against a prompt inside a sandbox."""

    @abstractmethod
    def version(self, container: Sandbox) -> str:
        """Return the version of the agent installed in the container."""

    @abstractmethod
    def run(self, container: Sandbox, prompt: str, timeout: int, max_turns: int) -> AgentResult:
        """Run the prompt in the container, stopping the agent after timeout seconds."""
