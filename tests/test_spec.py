from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from whichsdk.cli import STUDY_TEMPLATE
from whichsdk.spec import load_study

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID: dict[str, object] = {
    "name": "auth-nextjs",
    "category": "auth",
    "competitors": [
        {"name": "Clerk", "npm_packages": ["@clerk/nextjs"]},
        {"name": "Better Auth", "npm_packages": ["better-auth"]},
    ],
    "starter_template": "templates/nextjs-starter",
    "tasks": ["add login", "add social login"],
    "agents": [{"name": "claude_code", "model": "claude-sonnet-5-5"}],
    "repeats": 3,
    "timeout_seconds": 1800,
    "max_turns": 60,
    "run_budget_usd": 1.0,
    "study_budget_usd": 10.0,
}


def write_study(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "study.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_loads_a_valid_study(tmp_path: Path) -> None:
    spec = load_study(write_study(tmp_path, VALID))

    assert spec.name == "auth-nextjs"
    assert spec.category == "auth"
    assert [competitor.name for competitor in spec.competitors] == ["Clerk", "Better Auth"]
    assert spec.competitors[0].npm_packages == ["@clerk/nextjs"]
    assert spec.tasks == ["add login", "add social login"]
    assert (spec.repeats, spec.timeout_seconds, spec.max_turns) == (3, 1800, 60)
    assert (spec.run_budget_usd, spec.study_budget_usd) == (1.0, 10.0)


def test_each_competitor_package_maps_to_the_competitor_that_ships_it(tmp_path: Path) -> None:
    competitors = [
        {"name": "Clerk", "npm_packages": ["@clerk/nextjs", "@clerk/backend"]},
        {"name": "Better Auth", "npm_packages": ["better-auth"]},
    ]

    spec = load_study(write_study(tmp_path, {**VALID, "competitors": competitors}))

    assert spec.package_owners() == {
        "@clerk/nextjs": "Clerk",
        "@clerk/backend": "Clerk",
        "better-auth": "Better Auth",
    }


def test_each_agent_carries_its_model(tmp_path: Path) -> None:
    agents = [
        {"name": "claude_code", "model": "claude-sonnet-5-5"},
        {"name": "claude_code", "model": "claude-opus-5-5"},
    ]

    spec = load_study(write_study(tmp_path, {**VALID, "agents": agents}))

    assert [(agent.name, agent.model) for agent in spec.agents] == [
        ("claude_code", "claude-sonnet-5-5"),
        ("claude_code", "claude-opus-5-5"),
    ]


def test_starter_template_becomes_a_path(tmp_path: Path) -> None:
    spec = load_study(write_study(tmp_path, VALID))

    assert spec.starter_template == Path("templates") / "nextjs-starter"


def test_whole_number_budgets_are_accepted(tmp_path: Path) -> None:
    spec = load_study(write_study(tmp_path, {**VALID, "run_budget_usd": 1, "study_budget_usd": 10}))

    assert (spec.run_budget_usd, spec.study_budget_usd) == (1.0, 10.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Not A Slug"),
        ("name", "../escape"),
        ("name", ""),
        ("competitors", []),
        ("tasks", []),
        ("agents", []),
        ("agents", ["claude_code"]),
        ("agents", [{"name": "claude_code"}]),
        ("agents", [{"name": "claude_code", "model": ""}]),
        ("agents", [{"name": "codex", "model": "some-model"}]),
        ("repeats", 0),
        ("timeout_seconds", -1),
        ("max_turns", 0),
        ("run_budget_usd", 0),
        ("study_budget_usd", 0),
    ],
)
def test_rejects_an_invalid_value(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        load_study(write_study(tmp_path, {**VALID, field: value}))


def test_rejects_a_competitor_without_packages(tmp_path: Path) -> None:
    competitors = [{"name": "Clerk", "npm_packages": []}]

    with pytest.raises(ValidationError):
        load_study(write_study(tmp_path, {**VALID, "competitors": competitors}))


@pytest.mark.parametrize("field", ["run_budget_usd", "study_budget_usd"])
def test_rejects_a_missing_budget(tmp_path: Path, field: str) -> None:
    data = {key: value for key, value in VALID.items() if key != field}

    with pytest.raises(ValidationError, match=field):
        load_study(write_study(tmp_path, data))


def test_rejects_the_old_budget_field(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        load_study(write_study(tmp_path, {**VALID, "budget_usd": 5.0}))

    errors = [(error["loc"], error["type"]) for error in caught.value.errors()]
    assert errors == [(("budget_usd",), "extra_forbidden")]


def test_rejects_a_misspelled_field(tmp_path: Path) -> None:
    data = {**VALID, "repeat": 3}

    with pytest.raises(ValidationError, match="repeat"):
        load_study(write_study(tmp_path, data))


def test_rejects_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "study.yaml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_study(path)


def test_init_template_is_a_valid_study(tmp_path: Path) -> None:
    path = tmp_path / "new.yaml"
    path.write_text(STUDY_TEMPLATE.format(name="new-study"), encoding="utf-8")

    spec = load_study(path)

    assert spec.name == "new-study"
    assert spec.agents[0].model


def test_shipped_study_is_valid() -> None:
    spec = load_study(REPO_ROOT / "studies" / "auth-nextjs.yaml")

    assert {competitor.name for competitor in spec.competitors} == {
        "Clerk",
        "Better Auth",
        "Auth.js",
        "Supabase Auth",
    }
    assert len(spec.tasks) == 3
    assert [(agent.name, agent.model) for agent in spec.agents] == [
        ("claude_code", "claude-sonnet-5-5")
    ]
    assert (spec.run_budget_usd, spec.study_budget_usd) == (1.0, 10.0)
    assert (REPO_ROOT / spec.starter_template).is_dir()


def test_shipped_prompts_do_not_name_a_competitor() -> None:
    spec = load_study(REPO_ROOT / "studies" / "auth-nextjs.yaml")
    names = [
        term.lower()
        for competitor in spec.competitors
        for term in (competitor.name, *competitor.npm_packages)
    ]

    for task in spec.tasks:
        assert not [name for name in names if name in task.lower()]
