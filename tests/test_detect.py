import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from whichsdk import cli
from whichsdk.store import Run, Store

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
STUDY = "auth-nextjs"
SHIPPED_STUDY = yaml.safe_load(
    (REPO_ROOT / "studies" / "auth-nextjs.yaml").read_text(encoding="utf-8")
)


def write_study(**overrides: object) -> None:
    study = {**SHIPPED_STUDY, **overrides}
    Path("study.yaml").write_text(yaml.safe_dump(study), encoding="utf-8")


def detect() -> Result:
    return CliRunner().invoke(cli.app, ["detect", "study.yaml"])


def db(sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(cli.DB_PATH)) as connection:
        return connection.execute(sql).fetchall()


def save_attempt(
    store: Store,
    fixture: str,
    repeat: int = 0,
    build_passed: bool | None = True,
    where: str = "",
    failed: bool = False,
) -> Run:
    run = store.next_attempt(STUDY, 0, "claude_code", repeat)
    directory = Path("runs") / run.id / where
    shutil.copytree(FIXTURES / fixture, directory, dirs_exist_ok=True)
    if not (directory / "baseline").exists():
        (directory / "baseline").mkdir()
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(directory / name, directory / "baseline" / name)
    store.start_run(run, "test-model")
    store.begin_agent(run, "1.2.3")
    store.record_result(run, 0, 0.1, 5.0, directory / "transcript.jsonl")
    store.finish_run(run, "failed" if failed else "done")
    if build_passed is not None:
        store.add_event(run, "build_finished", {"passed": build_passed})
    return run


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    monkeypatch.chdir(tmp_path)
    write_study()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("detect must not start an agent or a container")

    monkeypatch.setattr(cli, "ClaudeCodeAgent", forbidden)
    monkeypatch.setattr(cli, "start_sandbox", forbidden)
    with closing(Store(cli.DB_PATH)) as saved:
        saved.save_study(STUDY, Path("study.yaml").read_text(encoding="utf-8"))
        yield saved


def test_the_real_run_built_its_own_auth_and_is_no_sdk(store: Store) -> None:
    run = save_attempt(store, "real")

    outcome = detect()

    assert outcome.exit_code == 0, outcome.output
    assert outcome.output.strip() == f"{run.id} attempt 1: no_sdk"
    assert db(
        "SELECT verdict, picked_competitor, added, removed, imported, switches, searched, "
        "build_passed, build_log_path FROM results"
    ) == [("no_sdk", None, "{}", "{}", "{}", "[]", 0, 1, f"runs/{run.id}/build.log")]


def test_packages_that_belong_to_no_competitor_are_other_and_stay_listed(store: Store) -> None:
    run = save_attempt(store, "other")

    outcome = detect()

    assert outcome.output.strip() == f"{run.id} attempt 1: other (bcryptjs, jose)"
    assert db("SELECT verdict, picked_competitor, added, imported FROM results") == [
        ("other", None, '{"bcryptjs": null, "jose": null}', "{}")
    ]


def test_a_run_that_wrote_no_code_is_not_no_sdk(store: Store) -> None:
    run = save_attempt(store, "real")
    (Path("runs") / run.id / "changes.diff").write_text("", encoding="utf-8")

    outcome = detect()

    assert outcome.output.strip() == f"{run.id} attempt 1: undetermined"


def test_a_changed_lockfile_is_not_no_sdk(store: Store) -> None:
    run = save_attempt(store, "real")
    lockfile = Path("runs") / run.id / "package-lock.json"
    lockfile.write_text(lockfile.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    outcome = detect()

    assert outcome.output.strip() == f"{run.id} attempt 1: undetermined"


def test_a_switch_is_found_and_the_competitor_that_was_kept_is_picked(store: Store) -> None:
    run = save_attempt(store, "switch")

    outcome = detect()

    assert outcome.output.strip() == f"{run.id} attempt 1: picked (Better Auth)"
    assert db(
        "SELECT verdict, picked_competitor, added, imported, switches, searched FROM results"
    ) == [
        (
            "picked",
            "Better Auth",
            '{"better-auth": "Better Auth"}',
            '{"better-auth": "Better Auth"}',
            '[{"from_competitor": "Auth.js", "to_competitor": "Better Auth"}]',
            1,
        )
    ]


def test_detection_can_be_fixed_and_rerun_without_an_agent(store: Store) -> None:
    save_attempt(store, "switch")
    write_study(competitors=[{"name": "Clerk", "npm_packages": ["@clerk/nextjs"]}])
    assert "other (better-auth)" in detect().output

    write_study()
    second = detect()

    assert "picked (Better Auth)" in second.output
    assert db("SELECT verdict FROM results") == [("picked",)]


def test_the_saved_build_result_is_kept(store: Store) -> None:
    save_attempt(store, "real", repeat=0, build_passed=False)
    save_attempt(store, "real", repeat=1, build_passed=None)

    detect()

    assert db("SELECT build_passed, build_log_path IS NULL FROM results ORDER BY run_id") == [
        (0, 0),
        (None, 1),
    ]


def test_an_attempt_without_a_diff_is_skipped(store: Store) -> None:
    run = save_attempt(store, "real")
    (Path("runs") / run.id / "changes.diff").unlink()

    outcome = detect()

    assert outcome.output.strip() == f"{run.id} attempt 1: skipped, no changes.diff was saved"
    assert db("SELECT COUNT(*) FROM results") == [(0,)]


def test_every_attempt_is_detected_wherever_its_files_were_moved_to(store: Store) -> None:
    first = save_attempt(store, "real", where="attempts/1", failed=True)
    second = save_attempt(store, "switch")

    outcome = detect()

    assert outcome.output.splitlines() == [
        f"{first.id} attempt 1: no_sdk",
        f"{second.id} attempt 2: picked (Better Auth)",
    ]
    assert db("SELECT attempt, verdict FROM results ORDER BY attempt") == [
        (1, "no_sdk"),
        (2, "picked"),
    ]


def test_attempts_that_never_ran_an_agent_are_left_out(store: Store) -> None:
    store.next_attempt(STUDY, 0, "claude_code", 0)

    outcome = detect()

    assert outcome.output.strip() == "No saved runs of auth-nextjs to detect."


def test_detect_needs_the_database_of_the_current_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_study()

    outcome = detect()

    assert outcome.exit_code == 1
    assert "whichsdk.sqlite" in outcome.output
    assert not cli.DB_PATH.exists()
