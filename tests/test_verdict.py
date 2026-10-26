from whichsdk.detect.deps import DependencyChanges
from whichsdk.detect.transcript import Switch, TranscriptFindings
from whichsdk.detect.verdict import Detection, judge

QUIET = TranscriptFindings([], False)


def judged(added: dict[str, str | None], imported: dict[str, str]) -> Detection:
    return judge(DependencyChanges(added, {}), imported, QUIET)


def test_one_competitor_added_and_imported_is_picked() -> None:
    detection = judged({"better-auth": "Better Auth", "zod": None}, {"better-auth": "Better Auth"})

    assert (detection.verdict, detection.picked) == ("picked", "Better Auth")


def test_a_competitor_with_several_packages_still_counts_once() -> None:
    added = {"@clerk/backend": "Clerk", "@clerk/nextjs": "Clerk"}

    detection = judged(added, {"@clerk/nextjs": "Clerk"})

    assert (detection.verdict, detection.picked) == ("picked", "Clerk")


def test_one_competitor_added_but_never_imported_is_installed_unused() -> None:
    detection = judged({"better-auth": "Better Auth"}, {})

    assert (detection.verdict, detection.picked) == ("installed_unused", None)


def test_importing_a_different_competitor_than_the_one_installed_is_installed_unused() -> None:
    detection = judged({"better-auth": "Better Auth"}, {"next-auth": "Auth.js"})

    assert (detection.verdict, detection.picked) == ("installed_unused", None)


def test_more_than_one_competitor_left_installed_is_multiple() -> None:
    added = {"better-auth": "Better Auth", "next-auth": "Auth.js"}

    detection = judged(added, {"better-auth": "Better Auth"})

    assert (detection.verdict, detection.picked) == ("multiple", None)


def test_nothing_detected_is_undetermined_never_none() -> None:
    assert judged({}, {}).verdict == "undetermined"


def test_imports_without_an_install_are_undetermined() -> None:
    assert judged({}, {"better-auth": "Better Auth"}).verdict == "undetermined"


def test_packages_that_belong_to_no_competitor_are_undetermined() -> None:
    assert judged({"jose": None, "bcryptjs": None}, {}).verdict == "undetermined"


def test_the_findings_are_carried_into_the_detection() -> None:
    switches = [Switch("Auth.js", "Better Auth")]
    changes = DependencyChanges({"better-auth": "Better Auth"}, {"next-auth": "Auth.js"})

    detection = judge(changes, {"better-auth": "Better Auth"}, TranscriptFindings(switches, True))

    assert detection.added == {"better-auth": "Better Auth"}
    assert detection.removed == {"next-auth": "Auth.js"}
    assert detection.imported == {"better-auth": "Better Auth"}
    assert detection.switches == switches
    assert detection.searched is True
