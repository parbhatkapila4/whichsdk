from dataclasses import dataclass
from pathlib import Path

from whichsdk.sandbox import Sandbox, with_timeout

BUILD_TIMEOUT_SECONDS = 600
LOG_LINES = 200


@dataclass(frozen=True)
class BuildResult:
    """Whether the build passed, and where the end of its output was saved."""

    passed: bool
    log_path: Path


def run_build(box: Sandbox, log_path: Path) -> BuildResult:
    """Run npm run build in the sandbox and save the last 200 lines of its output."""
    result = box.exec(with_timeout(BUILD_TIMEOUT_SECONDS, ["npm", "run", "build"]), check=False)
    tail = result.output.splitlines()[-LOG_LINES:]
    log_path.write_text("".join(f"{line}\n" for line in tail), encoding="utf-8", newline="\n")
    return BuildResult(result.exit_code == 0, log_path)
