from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from whichsdk.detect.build import run_build
from whichsdk.sandbox import ExecResult, Sandbox


class BuildSandbox(Sandbox):
    def __init__(self, exit_code: int, output: str) -> None:
        super().__init__("fake")
        self._result = ExecResult(exit_code, output)
        self.calls: list[tuple[list[str], dict[str, str], bool]] = []

    def exec(
        self, args: Sequence[str], env: Mapping[str, str] | None = None, check: bool = True
    ) -> ExecResult:
        self.calls.append((list(args), dict(env or {}), check))
        return self._result


def test_a_passing_build_is_recorded_with_its_output(tmp_path: Path) -> None:
    result = run_build(BuildSandbox(0, "compiling\nroutes\n"), tmp_path / "build.log")

    assert result.passed
    assert result.log_path == tmp_path / "build.log"
    assert result.log_path.read_text(encoding="utf-8") == "compiling\nroutes\n"


@pytest.mark.parametrize("exit_code", [1, 124])
def test_a_failed_or_timed_out_build_does_not_pass(tmp_path: Path, exit_code: int) -> None:
    result = run_build(BuildSandbox(exit_code, "error: boom\n"), tmp_path / "build.log")

    assert not result.passed
    assert result.log_path.read_text(encoding="utf-8") == "error: boom\n"


def test_only_the_last_200_lines_are_kept(tmp_path: Path) -> None:
    output = "".join(f"line {number}\n" for number in range(1, 251))

    result = run_build(BuildSandbox(1, output), tmp_path / "build.log")

    kept = result.log_path.read_text(encoding="utf-8").splitlines()
    assert kept == [f"line {number}" for number in range(51, 251)]


def test_a_build_with_no_output_leaves_an_empty_log(tmp_path: Path) -> None:
    result = run_build(BuildSandbox(0, ""), tmp_path / "build.log")

    assert result.log_path.read_text(encoding="utf-8") == ""


def test_the_build_runs_under_a_timeout_without_any_environment(tmp_path: Path) -> None:
    box = BuildSandbox(0, "")

    run_build(box, tmp_path / "build.log")

    expected = ["timeout", "--kill-after=30", "600", "npm", "run", "build"]
    assert box.calls == [(expected, {}, False)]
