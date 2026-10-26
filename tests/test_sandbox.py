import io
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from whichsdk import sandbox
from whichsdk.sandbox import ExecResult, Sandbox, start_sandbox, with_timeout


class DockerLog:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str], bytes | None]] = []

    def __call__(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> ExecResult:
        self.calls.append((args, dict(env or {}), stdin))
        return ExecResult(0, "")


class DiffSandbox(Sandbox):
    def __init__(self, diff: str) -> None:
        super().__init__("fake")
        self._diff = diff
        self.commands: list[list[str]] = []

    def exec(
        self, args: Sequence[str], env: Mapping[str, str] | None = None, check: bool = True
    ) -> ExecResult:
        self.commands.append(list(args))
        return ExecResult(0, self._diff if args[:2] == ["git", "diff"] else "")


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> DockerLog:
    log = DockerLog()
    monkeypatch.setattr(sandbox, "_docker", log)
    return log


@pytest.fixture
def starter(tmp_path: Path) -> Path:
    path = tmp_path / "starter"
    (path / "app").mkdir(parents=True)
    (path / "app" / "page.tsx").write_text("page", encoding="utf-8")
    (path / "package.json").write_text("{}", encoding="utf-8")
    (path / "node_modules").mkdir()
    (path / "node_modules" / "left.js").write_text("x", encoding="utf-8")
    (path / ".next").mkdir()
    return path


def test_with_timeout_wraps_the_command() -> None:
    wrapped = with_timeout(90, ["npm", "run", "build"])

    assert wrapped == ["timeout", "--kill-after=30", "90", "npm", "run", "build"]


def test_changes_are_staged_first_so_new_files_show_up() -> None:
    box = DiffSandbox("diff --git a/app/new.ts b/app/new.ts\n")

    changes = box.changes_since_baseline(["package-lock.json", "yarn.lock"])

    assert changes == "diff --git a/app/new.ts b/app/new.ts\n"
    assert box.commands == [
        ["git", "add", "-A"],
        [
            *["git", "diff", "--cached", "baseline", "--", "."],
            ":(exclude)package-lock.json",
            ":(exclude)yarn.lock",
        ],
    ]


def test_a_fresh_container_gets_the_starter_and_a_tagged_baseline_commit(
    docker: DockerLog, starter: Path
) -> None:
    with start_sandbox("box", starter):
        pass

    commands = [args for args, _, _ in docker.calls]
    assert commands[0] == ("rm", "-f", "box")
    assert commands[1] == (
        *("run", "-d", "--init", "--pull", "never", "--name", "box"),
        *("whichsdk-sandbox", "sleep", "infinity"),
    )
    assert commands[2] == ("cp", "-a", "-", "box:/workspace")
    assert [command[2:] for command in commands[3:-1]] == [
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.name", "whichsdk"),
        ("git", "config", "user.email", "whichsdk@localhost"),
        ("git", "add", "-A"),
        ("git", "commit", "-q", "-m", "baseline"),
        ("git", "tag", "baseline"),
    ]
    assert commands[-1] == ("rm", "-f", "box")


def test_the_starter_is_copied_without_generated_folders_and_owned_by_the_sandbox_user(
    docker: DockerLog, starter: Path
) -> None:
    with start_sandbox("box", starter):
        pass

    [archive] = [stdin for args, _, stdin in docker.calls if args[0] == "cp"]
    assert archive is not None
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = tar.getmembers()
    assert sorted(member.name for member in members) == ["app", "app/page.tsx", "package.json"]
    assert {(member.uid, member.gid, member.uname) for member in members} == {(1000, 1000, "node")}


def test_the_container_is_removed_even_when_the_run_breaks(
    docker: DockerLog, starter: Path
) -> None:
    with pytest.raises(RuntimeError), start_sandbox("box", starter):
        raise RuntimeError("the run broke")

    assert docker.calls[-1][0] == ("rm", "-f", "box")


def test_environment_values_reach_docker_through_the_environment_only(docker: DockerLog) -> None:
    Sandbox("box").exec(["claude", "-p", "hi"], env={"SECRET_NAME": "secret-value"})

    [(args, env, _)] = docker.calls
    assert args == ("exec", "-e", "SECRET_NAME", "box", "claude", "-p", "hi")
    assert env == {"SECRET_NAME": "secret-value"}
