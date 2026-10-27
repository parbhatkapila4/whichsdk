from dataclasses import replace

import pytest

from whichsdk.detect.deps import DependencyChanges
from whichsdk.detect.transcript import Switch, TranscriptFindings
from whichsdk.detect.verdict import Detection, judge

QUIET = TranscriptFindings([], False, False)
INSTALLED_SOMETHING = TranscriptFindings([], False, True)
UNTOUCHED = DependencyChanges({}, {}, {}, False)
BETTER_AUTH = {"better-auth": "Better Auth"}


def judged(added: dict[str, str | None], imported: dict[str, str]) -> Detection:
    return judge(DependencyChanges(added, {}, {}, bool(added)), imported, True, QUIET)


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
    added = {"better-auth": "Better Auth", "next-auth": "Auth.js"}

    detection = judged(added, BETTER_AUTH)

    assert (detection.verdict, detection.picked) == ("multiple", None)


def test_packages_that_belong_to_no_competitor_are_other_and_stay_listed() -> None:
    detection = judged({"jose": None, "bcryptjs": None}, {})

    assert (detection.verdict, detection.picked) == ("other", None)
    assert detection.added == {"jose": None, "bcryptjs": None}


def test_other_packages_next_to_a_competitor_do_not_make_it_other() -> None:
    assert judged({"better-auth": "Better Auth", "jose": None}, {}).verdict == "installed_unused"


def test_a_competitor_that_is_declared_but_not_locked_rules_out_other() -> None:
    changes = DependencyChanges({"jose": None}, {}, {"better-auth": "Better Auth"}, True)

    assert judge(changes, {}, True, QUIET).verdict == "undetermined"


def test_a_package_that_is_not_a_competitor_and_not_locked_leaves_other_alone() -> None:
    changes = DependencyChanges({"jose": None}, {}, {"zod": None}, True)

    assert judge(changes, {}, True, QUIET).verdict == "other"


def test_a_competitor_import_next_to_other_packages_is_undetermined() -> None:
    assert judged({"jose": None}, BETTER_AUTH).verdict == "undetermined"


def test_untouched_package_files_and_new_code_are_no_sdk() -> None:
    detection = judge(UNTOUCHED, {}, True, TranscriptFindings([], True, False))

    assert (detection.verdict, detection.picked) == ("no_sdk", None)
    assert (detection.added, detection.removed, detection.searched) == ({}, {}, True)


@pytest.mark.parametrize(
    ("changes", "imported", "wrote_code", "findings"),
    [
        (None, {}, True, QUIET),
        (UNTOUCHED, {}, False, QUIET),
        (replace(UNTOUCHED, package_files_changed=True), {}, True, QUIET),
        (DependencyChanges({}, {"react-dom": None}, {}, True), {}, True, QUIET),
        (UNTOUCHED, BETTER_AUTH, True, QUIET),
        (UNTOUCHED, {}, True, INSTALLED_SOMETHING),
    ],
    ids=[
        "package.json unreadable",
        "no code written",
        "package file changed",
        "package removed",
        "competitor imported but not installed",
        "install command in the transcript",
    ],
)
def test_no_sdk_needs_every_one_of_its_conditions(
    changes: DependencyChanges | None,
    imported: dict[str, str],
    wrote_code: bool,
    findings: TranscriptFindings,
) -> None:
    assert judge(changes, imported, wrote_code, findings).verdict == "undetermined"


def test_an_unreadable_package_json_leaves_nothing_added_or_removed() -> None:
    detection = judge(None, BETTER_AUTH, True, TranscriptFindings([], True, True))

    assert detection.verdict == "undetermined"
    assert (detection.added, detection.removed) == ({}, {})
    assert (detection.imported, detection.searched) == (BETTER_AUTH, True)


def test_imports_without_an_install_are_undetermined() -> None:
    assert judged({}, BETTER_AUTH).verdict == "undetermined"


def test_the_findings_are_carried_into_the_detection() -> None:
    switches = [Switch("Auth.js", "Better Auth")]
    changes = DependencyChanges(BETTER_AUTH, {"next-auth": "Auth.js"}, {}, True)

    detection = judge(changes, BETTER_AUTH, True, TranscriptFindings(switches, True, True))

    assert detection.added == BETTER_AUTH
    assert detection.removed == {"next-auth": "Auth.js"}
    assert detection.imported == BETTER_AUTH
    assert detection.switches == switches
    assert detection.searched is True
