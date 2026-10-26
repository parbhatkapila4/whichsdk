import io
import os
import subprocess
import tarfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

IMAGE = "whichsdk-sandbox"
WORKSPACE = PurePosixPath("/workspace")
SANDBOX_USER = "node"
SANDBOX_ID = 1000
GENERATED_DIRS = {"node_modules", ".next"}


class SandboxError(Exception):
    """A docker command failed."""


@dataclass(frozen=True)
class ExecResult:
    """Exit code and combined stdout and stderr of a finished command."""

    exit_code: int
    output: str


class Sandbox:
    """A running container holding the starter project with a git baseline commit."""

    def __init__(self, name: str) -> None:
        self.name = name

    def exec(
        self, args: Sequence[str], env: Mapping[str, str] | None = None, check: bool = True
    ) -> ExecResult:
        """Run a command in the workspace; env values never appear on the command line."""
        env = env or {}
        flags = [flag for variable in env for flag in ("-e", variable)]
        return _docker("exec", *flags, self.name, *args, env=env, check=check)

    def copy_out(self, names: Iterable[str], directory: Path) -> None:
        """Copy those of the named workspace files that exist into directory."""
        present = self.exec(["ls", "-1A"]).output.split("\n")
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            if name in present:
                _docker("cp", f"{self.name}:{WORKSPACE / name}", str(directory / name))


@contextmanager
def start_sandbox(name: str, starter: Path) -> Iterator[Sandbox]:
    """Start a fresh container with the starter committed as a baseline; remove it on exit."""
    _remove_container(name)
    _docker("run", "-d", "--init", "--pull", "never", "--name", name, IMAGE, "sleep", "infinity")
    try:
        sandbox = Sandbox(name)
        _docker("cp", "-a", "-", f"{name}:{WORKSPACE}", stdin=_starter_archive(starter))
        _commit_baseline(sandbox)
        yield sandbox
    finally:
        _remove_container(name)


def _remove_container(name: str) -> None:
    _docker("rm", "-f", name, check=False)


def _starter_archive(starter: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for entry in sorted(starter.iterdir()):
            if entry.name not in GENERATED_DIRS:
                archive.add(entry, arcname=entry.name, filter=_owned_by_sandbox_user)
    return buffer.getvalue()


def _owned_by_sandbox_user(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = SANDBOX_ID
    member.uname = member.gname = SANDBOX_USER
    return member


def _commit_baseline(sandbox: Sandbox) -> None:
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.name", "whichsdk"],
        ["git", "config", "user.email", "whichsdk@localhost"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "baseline"],
    ):
        sandbox.exec(command)


def _docker(
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    check: bool = True,
) -> ExecResult:
    process = subprocess.run(
        ["docker", *args],
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    result = ExecResult(process.returncode, process.stdout.decode("utf-8", errors="replace"))
    if check and result.exit_code != 0:
        command = " ".join(args)
        raise SandboxError(f"docker {command} exited {result.exit_code}: {result.output.strip()}")
    return result
