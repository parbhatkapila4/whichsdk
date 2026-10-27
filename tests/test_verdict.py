from dataclasses import replace
from itertools import product
from typing import get_args

import pytest

from whichsdk.detect.deps import DependencyChanges
from whichsdk.detect.transcript import Switch, TranscriptFindings
from whichsdk.detect.verdict import Detection, Reason, judge

QUIET = TranscriptFindings([], False, False, False)
INSTALLED_SOMETHING = TranscriptFindings([], False, True, False)
OUT_OF_TURNS = TranscriptFindings([], False, False, True)
UNTOUCHED = DependencyChanges({}, {}, {}, False)
BETTER_AUTH = {"better-auth": "Better Auth"}
BOTH = {"better-auth": "Better Auth", "next-auth": "Auth.js"}
UNLOCKED_COMPETITOR = DependencyChanges({}, {}, {"better-auth": "Better Auth"}, True)
PACKAGE_FILES_CHANGED = replace(UNTOUCHED, package_files_changed=True)
WHY_UNDETERMINED = [
    (UNTOUCHED, {}, True, QUIET, False, "run_failed"),
    (UNTOUCHED, {}, True, OUT_OF_TURNS, False, "max_turns"),
    (None, {}, True, QUIET, True, "package_json_unreadable"),
    (UNTOUCHED, BETTER_AUTH, True, QUIET, True, "competitor_import_without_install"),
    (UNLOCKED_COMPETITOR, {}, True, QUIET, True, "competitor_declared_without_lock"),
    (UNTOUCHED, {}, False, QUIET, True, "no_code_added"),
    (PACKAGE_FILES_CHANGED, {}, True, QUIET, True, "lockfile_changed_without_package"),
    (UNTOUCHED, {}, True, INSTALLED_SOMETHING, True, "install_command_changed_nothing"),
]


def judged(added: dict[str, str | None], imported: dict[str, str]) -> Detection:
    return judge(DependencyChanges(added, {}, {}, bool(added)), imported, True, QUIET, True)


def test_one_competitor_added_and_imported_is_picked() -> None:
    detection = judged({"better-auth": "Better Auth", "zod": None}, BETTER_AUTH)

    assert (detection.verdict, detection.picked) == ("picked", "Better Auth")


def test_a_competitor_with_several_packages_still_counts_once() -> None:
    added = {"@clerk/backend": "Clerk", "@clerk/nextjs": "Clerk"}

    detection = judged(added, {"@clerk/nextjs": "Clerk"})

    assert (detection.verdict, detection.picked) == ("picked", "Clerk")


def test_one_competitor_added_but_never_imported_is_installed_unused() -> None:
    detection = judged(BETTER_AUTH, {})

    assert (detection.verdict, detection.picked) == ("installed_unused", None)


def test_importing_a_different_competitor_than_the_one_installed_is_installed_unused() -> None:
    detection = judged(BETTER_AUTH, {"next-auth": "Auth.js"})

    assert (detection.verdict, detection.picked) == ("installed_unused", None)


def test_more_than_one_competitor_left_installed_is_multiple() -> None:
    detection = judged(BOTH, BETTER_AUTH)

    assert (detection.verdict, detection.picked) == ("multiple", None)


def test_packages_that_belong_to_no_competitor_are_other_and_stay_listed() -> None:
    detection = judged({"jose": None, "bcryptjs": None}, {})

    assert (detection.verdict, detection.picked) == ("other", None)
    assert detection.added == {"jose": None, "bcryptjs": None}


def test_other_packages_next_to_a_competitor_do_not_make_it_other() -> None:
    assert judged({"better-auth": "Better Auth", "jose": None}, {}).verdict == "installed_unused"


def test_a_competitor_that_is_declared_but_not_locked_rules_out_other() -> None:
    changes = DependencyChanges({"jose": None}, {}, {"better-auth": "Better Auth"}, True)

    assert judge(changes, {}, True, QUIET, True).verdict == "undetermined"


def test_a_package_that_is_not_a_competitor_and_not_locked_leaves_other_alone() -> None:
    changes = DependencyChanges({"jose": None}, {}, {"zod": None}, True)

    assert judge(changes, {}, True, QUIET, True).verdict == "other"


def test_a_competitor_import_next_to_other_packages_is_undetermined() -> None:
    assert judged({"jose": None}, BETTER_AUTH).verdict == "undetermined"


def test_untouched_package_files_and_new_code_are_no_sdk() -> None:
    detection = judge(UNTOUCHED, {}, True, TranscriptFindings([], True, False, False), True)

    assert (detection.verdict, detection.picked) == ("no_sdk", None)
    assert (detection.added, detection.removed, detection.searched) == ({}, {}, True)


@pytest.mark.parametrize(
    ("changes", "imported", "wrote_code", "findings", "finished"),
    [
        (None, {}, True, QUIET, True),
        (UNTOUCHED, {}, False, QUIET, True),
        (PACKAGE_FILES_CHANGED, {}, True, QUIET, True),
        (DependencyChanges({}, {"react-dom": None}, {}, True), {}, True, QUIET, True),
        (UNTOUCHED, BETTER_AUTH, True, QUIET, True),
        (UNTOUCHED, {}, True, INSTALLED_SOMETHING, True),
        (UNTOUCHED, {}, True, QUIET, False),
    ],
    ids=[
        "package.json unreadable",
        "no code written",
        "package file changed",
        "package removed",
        "competitor imported but not installed",
        "install command in the transcript",
        "run did not finish",
    ],
)
def test_no_sdk_needs_every_one_of_its_conditions(
    changes: DependencyChanges | None,
    imported: dict[str, str],
    wrote_code: bool,
    findings: TranscriptFindings,
    finished: bool,
) -> None:
    assert judge(changes, imported, wrote_code, findings, finished).verdict == "undetermined"


@pytest.mark.parametrize(
    ("changes", "imported"),
    [
        (DependencyChanges(BETTER_AUTH, {}, {}, True), BETTER_AUTH),
        (DependencyChanges(BETTER_AUTH, {}, {}, True), {}),
        (DependencyChanges(BOTH, {}, {}, True), {}),
        (DependencyChanges({"jose": None}, {}, {}, True), {}),
        (UNTOUCHED, {}),
        (None, {}),
    ],
    ids=["picked", "installed_unused", "multiple", "other", "no_sdk", "unreadable"],
)
def test_a_run_that_did_not_finish_is_undetermined_whatever_it_left_behind(
    changes: DependencyChanges | None, imported: dict[str, str]
) -> None:
    detection = judge(changes, imported, True, QUIET, False)

    assert (detection.verdict, detection.picked, detection.reason) == (
        "undetermined",
        None,
        "run_failed",
    )
    assert detection.added == ({} if changes is None else changes.added)
    assert detection.imported == imported


def test_the_turn_limit_only_names_the_reason_of_a_run_that_did_not_finish() -> None:
    assert judge(UNTOUCHED, {}, True, OUT_OF_TURNS, False).reason == "max_turns"
    assert judge(UNTOUCHED, {}, True, OUT_OF_TURNS, True).verdict == "no_sdk"


@pytest.mark.parametrize(
    ("changes", "imported", "wrote_code", "findings", "finished", "reason"),
    WHY_UNDETERMINED,
    ids=[case[-1] for case in WHY_UNDETERMINED],
)
def test_every_undetermined_verdict_says_why(
    changes: DependencyChanges | None,
    imported: dict[str, str],
    wrote_code: bool,
    findings: TranscriptFindings,
    finished: bool,
    reason: str,
) -> None:
    detection = judge(changes, imported, wrote_code, findings, finished)

    assert (detection.verdict, detection.reason) == ("undetermined", reason)


def test_the_cases_cover_every_reason() -> None:
    assert {case[-1] for case in WHY_UNDETERMINED} == set(get_args(Reason))


@pytest.mark.parametrize(
    ("changes", "imported", "wrote_code", "findings", "finished", "reason"),
    [
        (None, BETTER_AUTH, False, INSTALLED_SOMETHING, False, "run_failed"),
        (None, BETTER_AUTH, False, INSTALLED_SOMETHING, True, "package_json_unreadable"),
        (
            replace(UNLOCKED_COMPETITOR, package_files_changed=True),
            BETTER_AUTH,
            False,
            INSTALLED_SOMETHING,
            True,
            "competitor_import_without_install",
        ),
        (
            replace(UNLOCKED_COMPETITOR, package_files_changed=True),
            {},
            False,
            INSTALLED_SOMETHING,
            True,
            "competitor_declared_without_lock",
        ),
        (PACKAGE_FILES_CHANGED, {}, False, INSTALLED_SOMETHING, True, "no_code_added"),
        (
            PACKAGE_FILES_CHANGED,
            {},
            True,
            INSTALLED_SOMETHING,
            True,
            "lockfile_changed_without_package",
        ),
    ],
    ids=["unfinished", "unreadable", "imported", "unlocked", "no code", "package files"],
)
def test_a_run_with_several_problems_gives_the_first_reason(
    changes: DependencyChanges | None,
    imported: dict[str, str],
    wrote_code: bool,
    findings: TranscriptFindings,
    finished: bool,
    reason: str,
) -> None:
    assert judge(changes, imported, wrote_code, findings, finished).reason == reason


def test_only_an_undetermined_verdict_has_a_reason_and_only_picked_has_a_competitor() -> None:
    every_changes = [
        None,
        UNTOUCHED,
        PACKAGE_FILES_CHANGED,
        DependencyChanges({}, {"react-dom": None}, {}, True),
        DependencyChanges(BETTER_AUTH, {}, {}, True),
        DependencyChanges(BOTH, {}, {}, True),
        DependencyChanges({"jose": None}, {}, {}, True),
        UNLOCKED_COMPETITOR,
    ]
    every_findings = [QUIET, INSTALLED_SOMETHING, OUT_OF_TURNS]
    seen = set()

    for changes, imported, wrote_code, findings, finished in product(
        every_changes, [{}, BETTER_AUTH], [True, False], every_findings, [True, False]
    ):
        detection = judge(changes, imported, wrote_code, findings, finished)
        seen.add(detection.verdict)
        assert (detection.verdict == "undetermined") == (detection.reason is not None)
        assert (detection.verdict == "picked") == (detection.picked is not None)

    assert seen == {"picked", "installed_unused", "multiple", "other", "no_sdk", "undetermined"}


def test_an_unreadable_package_json_leaves_nothing_added_or_removed() -> None:
    detection = judge(None, BETTER_AUTH, True, TranscriptFindings([], True, True, False), True)

    assert detection.verdict == "undetermined"
    assert (detection.added, detection.removed) == ({}, {})
    assert (detection.imported, detection.searched) == (BETTER_AUTH, True)


def test_imports_without_an_install_are_undetermined() -> None:
    assert judged({}, BETTER_AUTH).verdict == "undetermined"


def test_the_findings_are_carried_into_the_detection() -> None:
    switches = [Switch("Auth.js", "Better Auth")]
    changes = DependencyChanges(BETTER_AUTH, {"next-auth": "Auth.js"}, {}, True)

    detection = judge(
        changes, BETTER_AUTH, True, TranscriptFindings(switches, True, True, False), True
    )

    assert detection.added == BETTER_AUTH
    assert detection.removed == {"next-auth": "Auth.js"}
    assert detection.imported == BETTER_AUTH
    assert detection.switches == switches
    assert detection.searched is True
