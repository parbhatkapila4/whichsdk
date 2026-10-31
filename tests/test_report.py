import json
import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from whichsdk import cli
from whichsdk.detect.build import BuildResult
from whichsdk.detect.transcript import Switch
from whichsdk.detect.verdict import Detection
from whichsdk.report import Report, build_report, render_html, report_json
from whichsdk.spec import StudySpec
from whichsdk.store import Outcome, Run, Spend, Store

STUDY: dict[str, object] = {
    "name": "demo",
    "category": "auth",
    "competitors": [
        {"name": "Clerk", "npm_packages": ["@clerk/nextjs"]},
        {"name": "Better Auth", "npm_packages": ["better-auth"]},
        {"name": "Auth.js", "npm_packages": ["next-auth"]},
    ],
    "helper_packages": ["server-only"],
    "starter_template": "starter",
    "tasks": ["alpha", "beta & <gamma>"],
    "agents": [{"name": "claude_code", "model": "test-model"}],
    "repeats": 3,
    "timeout_seconds": 600,
    "max_turns": 10,
    "run_budget_usd": 1.0,
    "study_budget_usd": 20.0,
}
CLERK = Detection("picked", "Clerk", None, {"@clerk/nextjs": "Clerk"}, {}, {}, [], False)
SWITCHED = Detection(
    "picked",
    "Better Auth",
    None,
    {"better-auth": "Better Auth"},
    {},
    {},
    [Switch("Clerk", "Better Auth")],
    False,
)
NO_SDK = Detection("no_sdk", None, None, {"server-only": None}, {}, {}, [], False)
RAN_OUT_OF_TURNS = Detection("undetermined", None, "max_turns", {}, {}, {}, [], False)
CRASHED = Detection("undetermined", None, "run_failed", {}, {}, {}, [], False)


def write_study(**overrides: object) -> None:
    Path("study.yaml").write_text(yaml.safe_dump({**STUDY, **overrides}), encoding="utf-8")


def report(*args: str) -> Result:
    return CliRunner().invoke(cli.app, ["report", "study.yaml", *args])


def finish(
    store: Store,
    task: int,
    repeat: int,
    detection: Detection,
    build_passed: bool | None,
    cost: float = 0.2,
    exit_code: int = 0,
) -> None:
    run = store.next_attempt("demo", task, "claude_code", repeat)
    store.start_run(run, "test-model")
    store.begin_agent(run, "9.9.9")
    store.record_result(run, exit_code, cost, 3.0, Path("runs") / run.id / "transcript.jsonl")
    store.finish_run(run, "done" if exit_code == 0 else "failed")
    build = None if build_passed is None else BuildResult(build_passed, Path("build.log"))
    store.save_results(run, detection, build)


@pytest.fixture
def populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    write_study()
    with closing(Store(cli.DB_PATH)) as store:
        store.save_study("demo", "name: demo")
        finish(store, 0, 0, CLERK, True)
        finish(store, 0, 1, CLERK, True)
        finish(store, 0, 2, SWITCHED, False)
        finish(store, 1, 0, NO_SDK, True)
        finish(store, 1, 1, CRASHED, None, cost=0.4, exit_code=1)
        finish(store, 1, 1, NO_SDK, True)
        finish(store, 1, 2, RAN_OUT_OF_TURNS, None, cost=0.9, exit_code=1)
    return tmp_path


def load_report() -> dict[str, object]:
    return json.loads(Path("report.json").read_text(encoding="utf-8"))


def table_rows(page: str, caption: str) -> list[list[str]]:
    table = re.search(rf"<caption>{re.escape(caption)}</caption>.*?</table>", page, flags=re.S)
    assert table, caption
    rows = re.findall(r"<tr>(.*?)</tr>", table.group(0), flags=re.S)
    return [re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.S) for row in rows]


def pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def run_row(task: int, repeat: int) -> Run:
    return Run(
        id=f"real-t{task}-claude_code-r{repeat}",
        attempt=1,
        study="real",
        task_index=task,
        agent="claude_code",
        repeat=repeat,
        status="done",
        started_at="2026-10-28T07:00:00+00:00",
        agent_started_at="2026-10-28T07:00:05+00:00",
        finished_at="2026-10-30T14:29:16+00:00",
        exit_code=0,
        cost_usd=0.1,
        duration_s=5.0,
        transcript_path=None,
        model="test-model",
        claude_code_version="9.9.9",
    )


def real_study_outcomes() -> list[Outcome]:
    picked = Detection("picked", "Auth.js", None, {"next-auth": "Auth.js"}, {}, {}, [], False)
    return [
        Outcome(run_row(index // 5, index % 5), picked if index < 10 else NO_SDK, True)
        for index in range(50)
    ]


def real_study_spec() -> StudySpec:
    return StudySpec.model_validate(
        {
            **STUDY,
            "name": "real",
            "competitors": [
                {"name": "Clerk", "npm_packages": ["clerk"]},
                {"name": "Auth.js", "npm_packages": ["next-auth"]},
                {"name": "Kinde", "npm_packages": ["kinde"]},
            ],
            "tasks": [f"task {index}" for index in range(10)],
            "repeats": 5,
        }
    )


def test_the_real_studys_numbers_come_out_as_a_wilson_range_should() -> None:
    result = build_report(
        real_study_spec(), real_study_outcomes(), [], Spend(4.74, 4.74, 0), date(2026, 10, 30)
    )

    by_name = {row.name: row.picks for row in result.competitors}
    assert (result.no_sdk.runs, result.no_sdk.rate) == (40, 0.8)
    assert (result.no_sdk.low, result.no_sdk.high) == (0.6696, 0.8876)
    assert (by_name["Auth.js"].rate, by_name["Auth.js"].low, by_name["Auth.js"].high) == (
        0.2,
        0.1124,
        0.3304,
    )
    assert (by_name["Clerk"].runs, by_name["Clerk"].low, by_name["Clerk"].high) == (0, 0.0, 0.0714)
    assert [row.name for row in result.competitors] == ["Auth.js", "Clerk", "Kinde"]


def test_a_run_counts_by_its_newest_attempt_and_the_numbers_add_up(populated: Path) -> None:
    outcome = report()

    assert outcome.exit_code == 0, outcome.output
    data = load_report()
    assert data["total_runs"] == 6
    assert data["verdicts"] == {
        "picked": 3,
        "installed_unused": 0,
        "multiple": 0,
        "other": 0,
        "no_sdk": 2,
        "undetermined": 1,
    }
    assert data["no_sdk"] == {"runs": 2, "rate": 0.3333, "low": 0.0968, "high": 0.7}
    assert {row["name"]: row["picks"]["runs"] for row in data["competitors"]} == {
        "Clerk": 2,
        "Better Auth": 1,
        "Auth.js": 0,
    }
    assert data["methods"]["attempts"] == 7
    assert data["methods"]["total_cost_usd"] == 2.3
    assert outcome.output.strip() == "Wrote report.html and report.json for 6 run(s)."


def test_every_competitor_appears_zero_picks_included_with_its_range(populated: Path) -> None:
    report()

    data = load_report()
    zero = next(row for row in data["competitors"] if row["name"] == "Auth.js")
    assert zero["picks"]["runs"] == 0
    assert (zero["picks"]["low"], zero["picks"]["high"]) == (0.0, 0.3903)
    page = Path("report.html").read_text(encoding="utf-8")
    assert ["Auth.js", "0", "0%", "0% to 39%"] in table_rows(page, "Pick rate by competitor")


def test_the_html_shows_the_numbers_of_the_json(populated: Path) -> None:
    report()

    data = load_report()
    page = Path("report.html").read_text(encoding="utf-8")
    assert table_rows(page, "Pick rate by competitor")[1:] == [
        [
            row["name"],
            str(row["picks"]["runs"]),
            pct(row["picks"]["rate"]),
            f"{pct(row['picks']['low'])} to {pct(row['picks']['high'])}",
        ]
        for row in data["competitors"]
    ]
    no_sdk = data["no_sdk"]
    assert f'<p class="big">{pct(no_sdk["rate"])}</p>' in page
    assert f"{no_sdk['runs']} of {data['total_runs']}" in page
    assert f"{pct(no_sdk['low'])} to {pct(no_sdk['high'])}" in page
    assert table_rows(page, "Verdicts of all runs")[1:] == [
        [f"<code>{verdict}</code>", str(count), pct(count / data["total_runs"])]
        for verdict, count in data["verdicts"].items()
    ]
    assert f"${data['methods']['total_cost_usd']:.2f}" in page


def test_the_same_report_gives_the_same_page_and_the_same_json() -> None:
    args = (real_study_spec(), real_study_outcomes(), [], Spend(4.74, 4.74, 0), date(2026, 11, 2))

    first, second = build_report(*args), build_report(*args)

    assert render_html(first) == render_html(second)
    assert report_json(first) == report_json(second)
    assert json.loads(report_json(first))["methods"]["generated"] == "2026-11-02"


def test_the_page_needs_nothing_else_to_open(populated: Path) -> None:
    report()

    page = Path("report.html").read_text(encoding="utf-8")
    assert "<script" not in page.lower()
    assert "<link" not in page.lower()
    assert "@import" not in page
    assert re.findall(r"https?://", page) == []
    assert re.findall(r"url\((?!#hatch)", page) == []
    assert page.count("<svg") == 4
    assert page.startswith("<!doctype html>")


def test_a_switch_between_competitors_is_listed_with_its_run(populated: Path) -> None:
    report()

    data = load_report()
    assert data["switches"] == [
        {
            "run_id": "demo-t0-claude_code-r2",
            "task": 0,
            "repeat": 2,
            "from_competitor": "Clerk",
            "to_competitor": "Better Auth",
        }
    ]
    page = Path("report.html").read_text(encoding="utf-8")
    assert table_rows(page, "Switch events")[1:] == [
        ["<code>demo-t0-claude_code-r2</code>", "0", "2", "Clerk", "Better Auth"]
    ]


def test_a_study_without_a_switch_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_study()
    with closing(Store(cli.DB_PATH)) as store:
        store.save_study("demo", "name: demo")
        finish(store, 0, 0, CLERK, True)

    report()

    assert load_report()["switches"] == []
    page = Path("report.html").read_text(encoding="utf-8")
    assert "No run installed one competitor and then replaced it with another." in page


def test_what_each_task_got_is_counted_with_the_outcome_of_each_repeat(populated: Path) -> None:
    report()

    tasks = load_report()["tasks"]
    assert tasks[0] == {
        "index": 0,
        "prompt": "alpha",
        "runs": 3,
        "outcomes": {"Clerk": 2, "Better Auth": 1},
        "repeats": ["Clerk", "Clerk", "Better Auth"],
    }
    assert tasks[1]["outcomes"] == {"no_sdk": 2, "undetermined": 1}
    assert tasks[1]["repeats"] == ["no_sdk", "no_sdk", "undetermined"]


def test_build_pass_rates_count_only_the_runs_that_have_a_build_result(populated: Path) -> None:
    report()

    builds = {row["verdict"]: row for row in load_report()["builds"]}
    assert builds["picked"] == {
        "verdict": "picked",
        "runs": 3,
        "built": 3,
        "passed": 2,
        "pass_rate": 0.6667,
    }
    assert builds["no_sdk"]["pass_rate"] == 1.0
    assert (builds["undetermined"]["runs"], builds["undetermined"]["built"]) == (1, 0)
    assert builds["undetermined"]["pass_rate"] is None
    page = Path("report.html").read_text(encoding="utf-8")
    assert ["<code>undetermined</code>", "1", "0", "0", "no runs"] in table_rows(
        page, "npm run build, run once at the end of each run"
    )


def test_the_methods_say_how_the_study_was_run(populated: Path) -> None:
    report()

    methods = load_report()["methods"]
    assert methods["agents"] == ["claude_code"]
    assert methods["models"] == ["test-model"]
    assert methods["claude_code_versions"] == ["9.9.9"]
    assert methods["starter"] == "starter"
    assert (methods["tasks"], methods["repeats"], methods["expected_runs"]) == (2, 3, 6)
    assert methods["helper_packages"] == ["server-only"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", methods["generated"])
    page = Path("report.html").read_text(encoding="utf-8")
    for fact in ("claude_code", "test-model", "9.9.9", "starter", "server-only", "$2.30"):
        assert fact in page


def test_dates_come_from_the_runs_and_the_day_the_report_is_written() -> None:
    result = build_report(
        real_study_spec(), real_study_outcomes(), [], Spend(4.74, 4.74, 0), date(2026, 11, 2)
    )

    assert (result.methods.first_run, result.methods.last_run) == ("2026-10-28", "2026-10-30")
    assert result.methods.generated == "2026-11-02"


def test_text_from_the_study_is_escaped(populated: Path) -> None:
    report()

    page = Path("report.html").read_text(encoding="utf-8")
    assert "beta &amp; &lt;gamma&gt;" in page
    assert "beta & <gamma>" not in page


def test_the_json_goes_next_to_the_html_and_the_folders_are_made(populated: Path) -> None:
    outcome = report("--out", "out/deep/study.html")

    assert outcome.exit_code == 0, outcome.output
    assert Path("out/deep/study.html").is_file()
    assert json.loads(Path("out/deep/study.json").read_text(encoding="utf-8"))["study"] == "demo"
    assert not Path("report.html").exists()


def test_the_json_name_cannot_be_given_as_the_html_file(populated: Path) -> None:
    outcome = report("--out", "numbers.json")

    assert outcome.exit_code == 1
    assert "--out is the HTML file" in outcome.output
    assert not Path("numbers.json").exists()


def test_a_study_with_no_results_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_study()
    with closing(Store(cli.DB_PATH)) as store:
        store.save_study("demo", "name: demo")
        store.next_attempt("demo", 0, "claude_code", 0)

    outcome = report()

    assert outcome.exit_code == 1
    assert (
        outcome.output.strip() == "No run of demo has results yet, so there is nothing to report."
    )
    assert not Path("report.html").exists()


def test_report_needs_the_database_of_the_current_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_study()

    outcome = report()

    assert outcome.exit_code == 1
    assert "There is no whichsdk.sqlite here" in outcome.output
    assert not cli.DB_PATH.exists()


def test_report_changes_nothing_in_the_database(populated: Path) -> None:
    def dump() -> list[str]:
        with closing(sqlite3.connect(cli.DB_PATH)) as connection:
            return list(connection.iterdump())

    before = dump()

    report()

    assert dump() == before
    assert not Path("runs").exists()


def test_report_is_a_report_of_the_study_it_was_given(populated: Path) -> None:
    with closing(Store(cli.DB_PATH)) as store:
        store.save_study("other", "name: other")
        other_run = store.next_attempt("other", 0, "claude_code", 0)
        store.start_run(other_run, "test-model")
        store.finish_run(other_run, "done")
        store.save_results(other_run, NO_SDK, None)

    report()

    assert load_report()["total_runs"] == 6


def test_the_report_type_is_what_the_json_holds(populated: Path) -> None:
    report()

    assert set(load_report()) == set(Report.__dataclass_fields__)
