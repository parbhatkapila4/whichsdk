import re
from contextlib import closing
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from pydantic import ValidationError

from whichsdk.agents.base import Agent, AgentResult
from whichsdk.agents.claude_code import ClaudeCodeAgent
from whichsdk.sandbox import IMAGE, SandboxError, start_sandbox
from whichsdk.spec import NAME_PATTERN, AgentSpec, StudySpec, load_study
from whichsdk.store import Run, Store

DB_PATH = Path("whichsdk.sqlite")
ENV_FILE = Path(".env")
RUNS_DIR = Path("runs")
STUDIES_DIR = Path("studies")
ATTEMPTS_DIR = "attempts"
TRANSCRIPT_NAME = "transcript.jsonl"
PACKAGE_FILES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
)

IMAGE_BUILD_HINT = (
    f"Build the sandbox image first: docker build -f docker/Dockerfile -t {IMAGE} "
    "templates/nextjs-starter"
)

STUDY_TEMPLATE = """\
name: {name}
category: example
competitors:
  - name: Example SDK
    npm_packages:
      - example-sdk
starter_template: templates/nextjs-starter
tasks:
  - Describe what to build, the way a developer would ask, without naming any competitor.
agents:
  - name: claude_code
    model: claude-sonnet-5-5
repeats: 3
timeout_seconds: 1800
max_turns: 60
run_budget_usd: 1.0
study_budget_usd: 10.0
"""

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Study name, used as the file name.")],
) -> None:
    """Write a starter study file to studies/<name>.yaml."""
    if not re.fullmatch(NAME_PATTERN, name):
        _fail(
            "The name must be lowercase letters, digits, - and _, starting with a letter or digit."
        )
    path = STUDIES_DIR / f"{name}.yaml"
    if path.exists():
        _fail(f"{path} already exists.")
    STUDIES_DIR.mkdir(exist_ok=True)
    path.write_text(STUDY_TEMPLATE.format(name=name), encoding="utf-8", newline="\n")
    typer.echo(f"Wrote {path}")


@app.command(epilog=IMAGE_BUILD_HINT)
def run_one(
    study_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    task: Annotated[int, typer.Option(min=0, help="Index of the task to run, from 0.")],
    repeat: Annotated[
        int, typer.Option(min=0, help="Which repeat this is; use a new one to run a task again.")
    ] = 0,
) -> None:
    """Run one task of a study once and save the result to SQLite."""
    spec = _load_spec(study_file)
    _check_selection(spec, task, repeat)
    agent_spec = spec.agents[0]
    agent = _make_agent(agent_spec, spec.run_budget_usd)
    with closing(Store(DB_PATH)) as store:
        store.save_study(spec.name, study_file.read_text(encoding="utf-8"))
        run = store.next_attempt(spec.name, task, agent_spec.name, repeat)
        if run.status == "done":
            _fail(f"{run.id} is already done; pass a different --repeat to run it again.")
        _check_budget(store, spec)
        _execute(store, spec, agent_spec, run, agent)


def _load_spec(path: Path) -> StudySpec:
    try:
        return load_study(path)
    except (ValidationError, yaml.YAMLError) as error:
        _fail(f"{path} is not a valid study:\n{error}")


def _check_selection(spec: StudySpec, task: int, repeat: int) -> None:
    if task >= len(spec.tasks):
        _fail(f"--task {task} is out of range; {spec.name} has tasks 0 to {len(spec.tasks) - 1}.")
    if repeat >= spec.repeats:
        _fail(f"--repeat {repeat} is out of range; {spec.name} allows 0 to {spec.repeats - 1}.")
    if not spec.starter_template.is_dir():
        _fail(f"Starter template {spec.starter_template} is not a directory.")


def _make_agent(agent_spec: AgentSpec, run_budget_usd: float) -> Agent:
    try:
        return ClaudeCodeAgent(ENV_FILE, agent_spec.model, run_budget_usd)
    except (OSError, ValueError) as error:
        _fail(f"Cannot read the API key: {error}")


def _check_budget(store: Store, spec: StudySpec) -> None:
    spend = store.study_spend(spec.name, spec.run_budget_usd)
    summary = f"${spend.total_usd:.2f} of ${spec.study_budget_usd:.2f} spent on {spec.name}"
    if spend.unreported_attempts:
        summary += (
            f" ({spend.unreported_attempts} attempt(s) with no reported cost "
            f"counted at ${spec.run_budget_usd:.2f} each)"
        )
    if spend.total_usd + spec.run_budget_usd > spec.study_budget_usd:
        _fail(f"Not starting: {summary}, and this run could cost up to ${spec.run_budget_usd:.2f}.")
    typer.echo(f"{summary}; this run is capped at ${spec.run_budget_usd:.2f}.")


def _execute(store: Store, spec: StudySpec, agent_spec: AgentSpec, run: Run, agent: Agent) -> None:
    prompt = spec.tasks[run.task_index]
    run_dir = RUNS_DIR / run.id
    _archive_previous_attempt(store, run, run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    store.start_run(run, agent_spec.model)
    store.add_event(
        run,
        "run_start",
        {
            "prompt": prompt,
            "model": agent_spec.model,
            "timeout_seconds": spec.timeout_seconds,
            "max_turns": spec.max_turns,
            "run_budget_usd": spec.run_budget_usd,
        },
    )
    typer.echo(f"Starting attempt {run.attempt} of {run.id} for up to {spec.timeout_seconds}s.")
    try:
        result = _run_agent(store, spec, run, agent, prompt, run_dir)
    except SandboxError as error:
        store.add_event(run, "run_error", {"message": str(error)})
        store.finish_run(run, "failed")
        _fail(f"{run.id} failed: {error}")
    store.finish_run(run, "done" if result.exit_code == 0 else "failed")
    _report(store.get_run(run.id, run.attempt))


def _archive_previous_attempt(store: Store, run: Run, run_dir: Path) -> None:
    files = [path for path in run_dir.glob("*") if path.name != ATTEMPTS_DIR]
    if not files:
        return
    archive = run_dir / ATTEMPTS_DIR / str(run.attempt - 1)
    archive.mkdir(parents=True, exist_ok=True)
    for path in files:
        path.rename(archive / path.name)
    store.move_transcript(run.id, run.attempt - 1, archive / TRANSCRIPT_NAME)


def _run_agent(
    store: Store, spec: StudySpec, run: Run, agent: Agent, prompt: str, run_dir: Path
) -> AgentResult:
    with start_sandbox(f"whichsdk-{run.id}", spec.starter_template) as box:
        version = agent.version(box)
        box.copy_out(PACKAGE_FILES, run_dir / "baseline")
        store.begin_agent(run, version)
        result = agent.run(box, prompt, spec.timeout_seconds, spec.max_turns)
        transcript = _save_transcript(run_dir, result)
        store.record_result(run, result.exit_code, result.cost_usd, result.duration_s, transcript)
        box.copy_out(PACKAGE_FILES, run_dir)
    return result


def _save_transcript(run_dir: Path, result: AgentResult) -> Path:
    path = run_dir / TRANSCRIPT_NAME
    path.write_text(
        "".join(f"{line}\n" for line in result.transcript_lines), encoding="utf-8", newline="\n"
    )
    return path


def _report(run: Run) -> None:
    cost = "unknown" if run.cost_usd is None else f"${run.cost_usd:.2f}"
    typer.echo(
        f"{run.id} attempt {run.attempt}: {run.status}, agent exit code {run.exit_code}, "
        f"cost {cost}, {run.duration_s:.0f}s"
    )
    typer.echo(f"Transcript and package files are in {RUNS_DIR / run.id}")
    if run.status == "failed":
        raise typer.Exit(1)


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)
