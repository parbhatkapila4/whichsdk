import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from whichsdk.cli import PACKAGE_FILES
from whichsdk.detect.deps import (
    DependencyChanges,
    compare_dependencies,
    is_helper,
    package_name,
)

FIXTURES = Path(__file__).parent / "fixtures"
OWNERS = {"kleur": "Kleur", "left-pad": "Left Pad", "@types/ms": "Ms Types"}
UNCHANGED = DependencyChanges({}, {}, {}, False)


def compare(
    baseline: Path,
    final: Path,
    owners: Mapping[str, str] = OWNERS,
    helpers: Sequence[str] = (),
) -> DependencyChanges:
    changes = compare_dependencies(baseline, final, owners, PACKAGE_FILES, helpers)
    assert changes is not None
    return changes


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


def bump_a_version(directory: Path) -> None:
    package = directory / "package.json"
    package.write_bytes(package.read_bytes().replace(b"^2.1.3", b"^2.1.4"))


def add_a_script(directory: Path) -> None:
    declare(directory, "scripts", "lint", "next lint")


def rewrite_the_lockfile(directory: Path) -> None:
    lockfile = directory / "package-lock.json"
    lockfile.write_text(lockfile.read_text(encoding="utf-8") + "\n", encoding="utf-8")


def delete_the_lockfile(directory: Path) -> None:
    (directory / "package-lock.json").unlink()


def add_another_lockfile(directory: Path) -> None:
    shutil.copyfile(FIXTURES / "pnpm" / "final" / "pnpm-lock.yaml", directory / "pnpm-lock.yaml")


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn"])
def test_each_package_manager_shows_what_was_added_and_removed(manager: str) -> None:
    changes = compare(FIXTURES / manager / "baseline", FIXTURES / manager / "final")

    assert changes == DependencyChanges(
        added={"@types/ms": "Ms Types", "kleur": "Kleur"},
        removed={"left-pad": "Left Pad"},
        unlocked={},
        package_files_changed=True,
    )


@pytest.mark.parametrize(
    ("package", "patterns", "expected"),
    [
        ("server-only", ["server-only"], True),
        ("server-only-extra", ["server-only"], False),
        ("@types/node", ["@types/*"], True),
        ("@types/react-dom", ["@types/*"], True),
        ("types", ["@types/*"], False),
        ("@typesafe/x", ["@types/*"], False),
        ("nodemailer", ["server-only", "nodemailer"], True),
        ("nodemailer", [], False),
    ],
)
def test_a_helper_pattern_matches_a_name_exactly_or_through_a_star(
    package: str, patterns: list[str], expected: bool
) -> None:
    assert is_helper(package, patterns) is expected


def test_added_helpers_stay_in_added_and_are_listed_apart(tmp_path: Path) -> None:
    changes = compare(
        FIXTURES / "npm" / "baseline",
        FIXTURES / "npm" / "final",
        {"kleur": "Kleur"},
        helpers=["@types/*", "left-pad"],
    )

    assert changes.added == {"@types/ms": None, "kleur": "Kleur"}
    assert changes.helpers == {"@types/ms"}


def test_a_competitor_package_is_never_a_helper_even_when_a_pattern_matches_it() -> None:
    changes = compare(
        FIXTURES / "npm" / "baseline", FIXTURES / "npm" / "final", helpers=["@types/*", "kleur"]
    )

    assert changes.added == {"@types/ms": "Ms Types", "kleur": "Kleur"}
    assert changes.helpers == frozenset()


def test_packages_that_belong_to_no_competitor_map_to_none() -> None:
    changes = compare(FIXTURES / "npm" / "baseline", FIXTURES / "npm" / "final", {"kleur": "Kleur"})

    assert changes.added == {"@types/ms": None, "kleur": "Kleur"}
    assert changes.removed == {"left-pad": None}


def test_a_package_listed_in_package_json_but_not_locked_is_not_installed(tmp_path: Path) -> None:
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    declare(final, "dependencies", "left-pad", "^1.3.0")

    changes = compare(FIXTURES / "npm" / "final", final)

    assert changes == DependencyChanges({}, {}, {"left-pad": "Left Pad"}, True)


def test_without_a_lockfile_package_json_decides(tmp_path: Path) -> None:
    baseline = copy_state(FIXTURES / "npm" / "baseline", tmp_path / "baseline")
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    (baseline / "package-lock.json").unlink()
    (final / "package-lock.json").unlink()
    declare(final, "dependencies", "left-pad", "^1.3.0")

    changes = compare(baseline, final)

    assert changes.added == {"@types/ms": "Ms Types", "kleur": "Kleur"}
    assert changes.removed == {}
    assert changes.unlocked == {}


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

    changes = compare(FIXTURES / "npm" / "baseline", final)

    assert changes.added == {"@types/ms": "Ms Types", "kleur": "Kleur"}
    assert changes.removed == {"left-pad": "Left Pad"}


def test_optional_dependencies_count(tmp_path: Path) -> None:
    baseline = write_package(tmp_path / "baseline", {"dependencies": {}})
    final = write_package(tmp_path / "final", {"optionalDependencies": {"kleur": "^4.1.5"}})

    changes = compare(baseline, final)

    assert changes.added == {"kleur": "Kleur"}


@pytest.mark.parametrize("unreadable", ["baseline", "final"])
def test_an_unreadable_package_json_cannot_be_compared(tmp_path: Path, unreadable: str) -> None:
    baseline = copy_state(FIXTURES / "npm" / "baseline", tmp_path / "baseline")
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    (tmp_path / unreadable / "package.json").write_text("{not json", encoding="utf-8")

    assert compare_dependencies(baseline, final, OWNERS, PACKAGE_FILES) is None


@pytest.mark.parametrize("missing", ["baseline", "final"])
def test_a_missing_package_json_cannot_be_compared(tmp_path: Path, missing: str) -> None:
    baseline = copy_state(FIXTURES / "npm" / "baseline", tmp_path / "baseline")
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    (tmp_path / missing / "package.json").unlink()

    assert compare_dependencies(baseline, final, OWNERS, PACKAGE_FILES) is None


def test_an_unreadable_lockfile_is_ignored_and_package_json_decides(tmp_path: Path) -> None:
    final = copy_state(FIXTURES / "pnpm" / "final", tmp_path / "final")
    (final / "pnpm-lock.yaml").write_text("packages: [unclosed", encoding="utf-8")

    changes = compare(FIXTURES / "pnpm" / "baseline", final)

    assert changes.added == {"@types/ms": "Ms Types", "kleur": "Kleur"}
    assert changes.removed == {"left-pad": "Left Pad"}


@pytest.mark.parametrize(
    "change",
    [
        bump_a_version,
        add_a_script,
        rewrite_the_lockfile,
        delete_the_lockfile,
        add_another_lockfile,
    ],
)
def test_any_difference_in_a_package_file_counts_as_a_change(
    tmp_path: Path, change: Callable[[Path], None]
) -> None:
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    change(final)

    assert compare(FIXTURES / "npm" / "final", final).package_files_changed is True


def test_a_change_to_a_file_that_is_not_a_package_file_does_not_count(tmp_path: Path) -> None:
    final = copy_state(FIXTURES / "npm" / "final", tmp_path / "final")
    (final / "notes.txt").write_text("changed", encoding="utf-8")

    assert compare(FIXTURES / "npm" / "final", final) == UNCHANGED


def test_a_package_that_was_already_unlocked_at_the_baseline_is_not_new(tmp_path: Path) -> None:
    baseline = copy_state(FIXTURES / "npm" / "final", tmp_path / "baseline")
    declare(baseline, "dependencies", "left-pad", "^1.3.0")
    final = copy_state(baseline, tmp_path / "final")

    assert compare(baseline, final) == UNCHANGED


def test_the_real_run_changed_no_package_file_and_its_real_lockfile_is_read(tmp_path: Path) -> None:
    real = FIXTURES / "real"
    baseline = tmp_path / "baseline"
    final = tmp_path / "final"
    for directory in (baseline, final):
        directory.mkdir()
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(real / name, directory / name)
    assert compare(baseline, final, {"next": "Next.js"}) == UNCHANGED

    declare(final, "dependencies", "zod", "^3.0.0")

    assert compare(baseline, final, {"zod": "Zod"}) == DependencyChanges(
        {}, {}, {"zod": "Zod"}, True
    )


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
