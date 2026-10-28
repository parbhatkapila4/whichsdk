import re
import threading
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn, get_args

import typer
import yaml
from pydantic import ValidationError

from whichsdk.agents.base import Agent, AgentResult
from whichsdk.agents.claude_code import ClaudeCodeAgent
from whichsdk.detect.build import BuildResult, run_build
from whichsdk.detect.deps import compare_dependencies
from whichsdk.detect.imports import adds_source, find_imports
from whichsdk.detect.transcript import read_transcript
from whichsdk.detect.verdict import Detection, Verdict, judge
from whichsdk.sandbox import IMAGE, Sandbox, SandboxError, remove_container, start_sandbox
from whichsdk.spec import NAME_PATTERN, AgentSpec, StudySpec, load_study
from whichsdk.store import Run, Spend, Status, Store

DB_PATH = Path("whichsdk.sqlite")
ENV_FILE = Path(".env")
RUNS_DIR = Path("runs")
STUDIES_DIR = Path("studies")
ATTEMPTS_DIR = "attempts"
BASELINE_DIR = "baseline"
TRANSCRIPT_NAME = "transcript.jsonl"
CHANGES_NAME = "changes.diff"
BUILD_LOG_NAME = "build.log"
LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
PACKAGE_FILES = ("package.json", *LOCKFILES)
POLL_SECONDS = 1.0

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


@dataclass(frozen=True)
class _Slot:
    task: int
    repeat: int
    agent: AgentSpec


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
        _begin(store, spec, agent_spec, run)
        typer.echo(f"Starting attempt {run.attempt} of {run.id} for up to {spec.timeout_seconds}s.")
        try:
            detection, build = _execute(store, spec, run, agent, threading.Event())
        except SandboxError as error:
            _fail(f"{run.id} failed: {error}")
        _report(store.get_run(run.id, run.attempt), detection, build)


@app.command("run", epilog=IMAGE_BUILD_HINT)
def run_study(
    study_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    concurrency: Annotated[
        int, typer.Option(min=1, help="How many runs to have going at once.")
    ] = 1,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="Also run the runs that failed.")
    ] = False,
) -> None:
    """Run every task, repeat and agent of a study that is not done yet.

    Failed runs are skipped unless --retry-failed is passed. Ctrl-C marks the runs in flight failed.
    """
    spec = _load_spec(study_file)
    _check_starter(spec)
    with closing(Store(DB_PATH)) as store:
        store.save_study(spec.name, study_file.read_text(encoding="utf-8"))
        states = _slot_states(store, spec)
        queue = _to_run(states, retry_failed)
        typer.echo(f"{spec.name}: {len(queue)} run(s) to start, up to {concurrency} at a time.")
        failed = sum(state in ("failed", "running") for _, state in states)
        if failed and not retry_failed:
            typer.echo(f"Skipping {failed} failed run(s); pass --retry-failed to run them again.")
        if not queue:
            return
        agents = {
            agent_spec.name: _make_agent(agent_spec, spec.run_budget_usd)
            for agent_spec in spec.agents
        }
        try:
            finished, not_started = _run_queue(store, spec, agents, queue, concurrency)
        except KeyboardInterrupt:
            typer.echo(
                "Interrupted: the runs in flight are marked failed, their containers removed."
            )
            raise typer.Exit(130) from None
        _summarize(store, spec, finished, not_started)


@app.command()
def status(
    study_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Show how many runs of a study are done, failed and pending, what it cost and its verdicts."""
    spec = _load_spec(study_file)
    with closing(Store(DB_PATH if DB_PATH.is_file() else Path(":memory:"))) as store:
        states = Counter(state for _, state in _slot_states(store, spec))
        spend = store.study_spend(spec.name, spec.run_budget_usd)
        verdicts = store.verdict_counts(spec.name)
    typer.echo(
        f"{spec.name}: {states.total()} runs "
        f"({len(spec.tasks)} tasks x {spec.repeats} repeats x {len(spec.agents)} agent(s))"
    )
    typer.echo(
        f"done {states['done']}, failed {states['failed']}, "
        f"running {states['running']}, pending {states['pending']}"
    )
    typer.echo(_spend_summary(spec, spend))
    typer.echo("Verdicts of the newest attempt of each run:")
    width = max(len(verdict) for verdict in get_args(Verdict))
    for verdict in get_args(Verdict):
        typer.echo(f"  {verdict:<{width}}  {verdicts.get(verdict, 0)}")


@app.command()
def detect(
    study_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Re-run the detectors on a study's saved runs without calling any agent.

    The saved build results are kept, because the container they ran in is gone.
    """
    spec = _load_spec(study_file)
    if not DB_PATH.is_file():
        _fail(f"There is no {DB_PATH} here; run this from the directory the study was run in.")
    with closing(Store(DB_PATH)) as store:
        lines = [
            _detect_saved(store, spec, run, Path(run.transcript_path).parent)
            for run in store.study_attempts(spec.name)
            if run.transcript_path
        ]
    typer.echo("\n".join(lines) or f"No saved runs of {spec.name} to detect.")


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
    _check_starter(spec)


def _check_starter(spec: StudySpec) -> None:
    if not spec.starter_template.is_dir():
        _fail(f"Starter template {spec.starter_template} is not a directory.")


def _make_agent(agent_spec: AgentSpec, run_budget_usd: float) -> Agent:
    try:
        return ClaudeCodeAgent(ENV_FILE, agent_spec.model, run_budget_usd)
    except (OSError, ValueError) as error:
        _fail(f"Cannot read the API key: {error}")


def _spend_summary(spec: StudySpec, spend: Spend) -> str:
    summary = f"${spend.total_usd:.2f} of ${spec.study_budget_usd:.2f} spent on {spec.name}"
    if spend.unreported_attempts:
        summary += (
            f" ({spend.unreported_attempts} attempt(s) with no reported cost "
            f"counted at ${spec.run_budget_usd:.2f} each)"
        )
    return summary


def _over_budget(spec: StudySpec, spend: Spend) -> bool:
    return spend.total_usd + spec.run_budget_usd > spec.study_budget_usd


def _check_budget(store: Store, spec: StudySpec) -> None:
    spend = store.study_spend(spec.name, spec.run_budget_usd)
    summary = _spend_summary(spec, spend)
    if _over_budget(spec, spend):
        _fail(f"Not starting: {summary}, and this run could cost up to ${spec.run_budget_usd:.2f}.")
    typer.echo(f"{summary}; this run is capped at ${spec.run_budget_usd:.2f}.")


def _slots(spec: StudySpec) -> list[_Slot]:
    return [
        _Slot(task, repeat, agent_spec)
        for repeat in range(spec.repeats)
        for task in range(len(spec.tasks))
        for agent_spec in spec.agents
    ]


def _slot_states(store: Store, spec: StudySpec) -> list[tuple[_Slot, Status]]:
    newest = {
        (run.task_index, run.agent, run.repeat): run.status
        for run in store.study_attempts(spec.name)
    }
    return [
        (slot, newest.get((slot.task, slot.agent.name, slot.repeat), "pending"))
        for slot in _slots(spec)
    ]


def _to_run(states: list[tuple[_Slot, Status]], retry_failed: bool) -> list[_Slot]:
    wanted = {"pending", "failed", "running"} if retry_failed else {"pending"}
    return [slot for slot, state in states if state in wanted]


def _run_queue(
    store: Store,
    spec: StudySpec,
    agents: Mapping[str, Agent],
    queue: list[_Slot],
    concurrency: int,
) -> tuple[list[Run], int]:
    finished: list[Run] = []
    in_flight: dict[Future[str], Run] = {}
    interrupted = threading.Event()
    reported = store.study_spend(spec.name, spec.run_budget_usd).reported_usd
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        try:
            while queue or in_flight:
                while queue and len(in_flight) < concurrency and _may_start(store, spec):
                    run = _start(store, spec, queue.pop(0))
                    in_flight[pool.submit(_work, spec, run, agents[run.agent], interrupted)] = run
                if not in_flight:
                    break
                done, _ = wait(in_flight, timeout=POLL_SECONDS, return_when=FIRST_COMPLETED)
                for future in done:
                    started = in_flight.pop(future)
                    final = store.get_run(started.id, started.attempt)
                    finished.append(final)
                    reported += final.cost_usd or 0.0
                    typer.echo(_finished_line(final, future.result(), reported))
        finally:
            _stop_in_flight(in_flight, interrupted)
    return finished, len(queue)


def _may_start(store: Store, spec: StudySpec) -> bool:
    return not _over_budget(spec, store.study_spend(spec.name, spec.run_budget_usd))


def _start(store: Store, spec: StudySpec, slot: _Slot) -> Run:
    run = store.next_attempt(spec.name, slot.task, slot.agent.name, slot.repeat)
    _begin(store, spec, slot.agent, run)
    return run


def _work(spec: StudySpec, run: Run, agent: Agent, interrupted: threading.Event) -> str:
    with closing(Store(DB_PATH)) as store:
        try:
            detection, _ = _execute(store, spec, run, agent, interrupted)
        except SandboxError as error:
            return f"failed ({' '.join(str(error).split())})"
    return _describe(detection)


def _stop_in_flight(in_flight: Mapping[Future[str], Run], interrupted: threading.Event) -> None:
    interrupted.set()
    while not all(future.done() for future in in_flight):
        for future, run in in_flight.items():
            if not future.done():
                remove_container(_container_name(run))
        wait(in_flight, timeout=POLL_SECONDS)


def _finished_line(run: Run, description: str, reported_usd: float) -> str:
    return (
        f"task {run.task_index} repeat {run.repeat} {run.agent}: {description}, "
        f"cost {_cost_text(run.cost_usd)}, total ${reported_usd:.2f}"
    )


def _summarize(store: Store, spec: StudySpec, finished: list[Run], not_started: int) -> None:
    failed = sum(run.status == "failed" for run in finished)
    typer.echo(f"Ran {len(finished)} attempt(s): {len(finished) - failed} done, {failed} failed.")
    if not_started:
        summary = _spend_summary(spec, store.study_spend(spec.name, spec.run_budget_usd))
        typer.echo(
            f"Stopped with {not_started} run(s) not started: {summary}, "
            f"and the next run could cost up to ${spec.run_budget_usd:.2f}."
        )
    if failed or not_started:
        raise typer.Exit(1)


def _container_name(run: Run) -> str:
    return f"whichsdk-{run.id}"


def _begin(store: Store, spec: StudySpec, agent_spec: AgentSpec, run: Run) -> None:
    run_dir = RUNS_DIR / run.id
    _archive_previous_attempt(store, run, run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    store.start_run(run, agent_spec.model)
    store.add_event(
        run,
        "run_start",
        {
            "prompt": spec.tasks[run.task_index],
            "model": agent_spec.model,
            "timeout_seconds": spec.timeout_seconds,
            "max_turns": spec.max_turns,
            "run_budget_usd": spec.run_budget_usd,
        },
    )


def _execute(
    store: Store, spec: StudySpec, run: Run, agent: Agent, interrupted: threading.Event
) -> tuple[Detection, BuildResult]:
    run_dir = RUNS_DIR / run.id
    try:
        result, build = _run_agent(store, spec, run, agent, spec.tasks[run.task_index], run_dir)
    except SandboxError as error:
        kind = "run_interrupted" if interrupted.is_set() else "run_error"
        store.add_event(run, kind, {"message": str(error)})
        store.finish_run(run, "failed")
        raise
    cut_short = interrupted.is_set()
    if cut_short:
        store.add_event(run, "run_interrupted", {})
    finished = result.exit_code == 0 and not cut_short
    store.finish_run(run, "done" if finished else "failed")
    detection = _detect_attempt(spec, run_dir, finished)
    store.save_results(run, detection, build)
    return detection, build


def _archive_previous_attempt(store: Store, run: Run, run_dir: Path) -> None:
    files = [path for path in run_dir.glob("*") if path.name != ATTEMPTS_DIR]
    if not files:
        return
    archive = run_dir / ATTEMPTS_DIR / str(run.attempt - 1)
    archive.mkdir(parents=True, exist_ok=True)
    for path in files:
        path.rename(archive / path.name)
    store.move_transcript(run.id, run.attempt - 1, archive / TRANSCRIPT_NAME)
    store.move_build_log(run.id, run.attempt - 1, archive / BUILD_LOG_NAME)


def _run_agent(
    store: Store, spec: StudySpec, run: Run, agent: Agent, prompt: str, run_dir: Path
) -> tuple[AgentResult, BuildResult]:
    with start_sandbox(_container_name(run), spec.starter_template) as box:
        version = agent.version(box)
        box.copy_out(PACKAGE_FILES, run_dir / BASELINE_DIR)
        store.begin_agent(run, version)
        result = agent.run(box, prompt, spec.timeout_seconds, spec.max_turns)
        transcript = _save_transcript(run_dir, result)
        store.record_result(run, result.exit_code, result.cost_usd, result.duration_s, transcript)
        box.copy_out(PACKAGE_FILES, run_dir)
        _save_changes(box, run_dir)
        build = run_build(box, run_dir / BUILD_LOG_NAME)
        store.add_event(run, "build_finished", {"passed": build.passed})
    return result, build


def _save_transcript(run_dir: Path, result: AgentResult) -> Path:
    path = run_dir / TRANSCRIPT_NAME
    path.write_text(
        "".join(f"{line}\n" for line in result.transcript_lines), encoding="utf-8", newline="\n"
    )
    return path


def _save_changes(box: Sandbox, run_dir: Path) -> None:
    changes = box.changes_since_baseline(LOCKFILES)
    (run_dir / CHANGES_NAME).write_text(changes, encoding="utf-8", newline="\n")


def _detect_attempt(spec: StudySpec, attempt_dir: Path, finished: bool) -> Detection:
    owners = spec.package_owners()
    diff = attempt_dir / CHANGES_NAME
    changes = compare_dependencies(attempt_dir / BASELINE_DIR, attempt_dir, owners, PACKAGE_FILES)
    findings = read_transcript(attempt_dir / TRANSCRIPT_NAME, owners)
    return judge(changes, find_imports(diff, owners), adds_source(diff), findings, finished)


def _detect_saved(store: Store, spec: StudySpec, run: Run, attempt_dir: Path) -> str:
    label = f"{run.id} attempt {run.attempt}"
    if not (attempt_dir / CHANGES_NAME).is_file():
        return f"{label}: skipped, no {CHANGES_NAME} was saved"
    detection = _detect_attempt(spec, attempt_dir, run.status == "done")
    passed = store.build_passed(run)
    build = None if passed is None else BuildResult(passed, attempt_dir / BUILD_LOG_NAME)
    store.save_results(run, detection, build)
    return f"{label}: {_describe(detection)}"


def _describe(detection: Detection) -> str:
    if detection.picked is not None:
        return f"{detection.verdict} ({detection.picked})"
    if detection.verdict == "other":
        return f"{detection.verdict} ({', '.join(detection.added)})"
    if detection.reason is not None:
        return f"{detection.verdict} ({detection.reason})"
    return detection.verdict


def _cost_text(cost_usd: float | None) -> str:
    return "unknown" if cost_usd is None else f"${cost_usd:.2f}"


def _report(run: Run, detection: Detection, build: BuildResult) -> None:
    typer.echo(
        f"{run.id} attempt {run.attempt}: {run.status}, agent exit code {run.exit_code}, "
        f"cost {_cost_text(run.cost_usd)}, {run.duration_s:.0f}s"
    )
    typer.echo(f"Verdict: {_describe(detection)}; build {'passed' if build.passed else 'failed'}")
    typer.echo(f"Saved to {RUNS_DIR / run.id}")
    if run.status == "failed":
        raise typer.Exit(1)


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)
