import json
import shutil
from pathlib import Path

import pytest

from whichsdk.detect.deps import DependencyChanges, compare_dependencies, package_name

FIXTURES = Path(__file__).parent / "fixtures"
OWNERS = {"kleur": "Kleur", "left-pad": "Left Pad", "@types/ms": "Ms Types"}
NOTHING = DependencyChanges({}, {})


def copy_state(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def write_package(directory: Path, package: dict[str, object]) -> Path:
    directory.mkdir(exist_ok=True)
    (directory / "package.json").write_text(json.dumps(package), encoding="utf-8")
    return directory


def declare(directory: Path, section: str, name: str, version: str) -> None:
    package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    package.setdefault(section, {})[name] = version
    write_package(directory, package)


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn"])
def test_each_package_manager_shows_what_was_added_and_removed(manager: str) -> None:
    changes = compare_dependencies(
        FIXTURES / manager / "baseline", FIXTURES / manager / "final", OWNERS
    )

    assert changes.added == {"@types/ms": "Ms Types", "kleur": "Kleur"}
    assert changes.removed == {"left-pad": "Left Pad"}


def test_packages_that_belong_to_no_competitor_map_to_none() -> None:
    changes = compare_dependencies(
        FIXTURES / "npm" / "baseline", FIXTURES / "npm" / "final", {"kleur": "Kleur"}
    )

    assert changes.added == {"@types/ms": None, "kleur": "Kleur"}
    assert changes.removed == {"left-pad": None}


def test_a_package_listed_in_package_json_but_not_locked_is_not_installed(tmp_path: Path) -> None:
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    declare(final, "dependencies", "left-pad", "^1.3.0")

    changes = compare_dependencies(FIXTURES / "npm" / "final", final, OWNERS)

    assert changes == NOTHING


def test_without_a_lockfile_package_json_decides(tmp_path: Path) -> None:
    baseline = copy_state(FIXTURES / "npm" / "baseline", tmp_path / "baseline")
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    (baseline / "package-lock.json").unlink()
    (final / "package-lock.json").unlink()
    declare(final, "dependencies", "left-pad", "^1.3.0")

    changes = compare_dependencies(baseline, final, OWNERS)

    assert changes.added == {"@types/ms": "Ms Types", "kleur": "Kleur"}
    assert changes.removed == {}


@pytest.mark.parametrize(
    ("fresh", "stale", "stale_lockfile"),
    [
        ("npm", "pnpm", "pnpm-lock.yaml"),
        ("pnpm", "npm", "package-lock.json"),
        ("yarn", "npm", "package-lock.json"),
        ("npm", "yarn", "yarn.lock"),
    ],
)
def test_every_lockfile_that_exists_counts(
    tmp_path: Path, fresh: str, stale: str, stale_lockfile: str
) -> None:
    final = copy_state(FIXTURES / fresh / "final", tmp_path / "final")
    shutil.copyfile(FIXTURES / stale / "baseline" / stale_lockfile, final / stale_lockfile)

    changes = compare_dependencies(FIXTURES / "npm" / "baseline", final, OWNERS)

    assert changes.added == {"@types/ms": "Ms Types", "kleur": "Kleur"}
    assert changes.removed == {"left-pad": "Left Pad"}


def test_optional_dependencies_count(tmp_path: Path) -> None:
    baseline = write_package(tmp_path / "baseline", {"dependencies": {}})
    final = write_package(tmp_path / "final", {"optionalDependencies": {"kleur": "^4.1.5"}})

    changes = compare_dependencies(baseline, final, OWNERS)

    assert changes.added == {"kleur": "Kleur"}


def test_an_unreadable_final_package_json_means_nothing_is_detected(tmp_path: Path) -> None:
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    (final / "package.json").write_text("{not json", encoding="utf-8")

    assert compare_dependencies(FIXTURES / "npm" / "baseline", final, OWNERS) == NOTHING


def test_a_missing_package_json_means_nothing_is_detected(tmp_path: Path) -> None:
    assert compare_dependencies(FIXTURES / "npm" / "baseline", tmp_path, OWNERS) == NOTHING


def test_an_unreadable_lockfile_is_ignored_and_package_json_decides(tmp_path: Path) -> None:
    final = copy_state(FIXTURES / "pnpm" / "final", tmp_path / "final")
    (final / "pnpm-lock.yaml").write_text("packages: [unclosed", encoding="utf-8")

    changes = compare_dependencies(FIXTURES / "pnpm" / "baseline", final, OWNERS)

    assert changes.added == {"@types/ms": "Ms Types", "kleur": "Kleur"}
    assert changes.removed == {"left-pad": "Left Pad"}


def test_the_real_run_installed_nothing_and_its_real_lockfile_is_read(tmp_path: Path) -> None:
    real = FIXTURES / "real"
    baseline = tmp_path / "baseline"
    final = tmp_path / "final"
    for directory in (baseline, final):
        directory.mkdir()
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(real / name, directory / name)
    assert compare_dependencies(baseline, final, {"next": "Next.js"}) == NOTHING

    declare(final, "dependencies", "zod", "^3.0.0")

    assert compare_dependencies(baseline, final, {"zod": "Zod"}) == NOTHING


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        ("kleur", "kleur"),
        ("kleur@4.1.5", "kleur"),
        ("better-auth@latest", "better-auth"),
        ("@types/ms", "@types/ms"),
        ("@types/ms@^2.1.0", "@types/ms"),
        ("@clerk/nextjs@6", "@clerk/nextjs"),
    ],
)
def test_the_package_name_is_taken_from_a_spec(spec: str, name: str) -> None:
    assert package_name(spec) == name
