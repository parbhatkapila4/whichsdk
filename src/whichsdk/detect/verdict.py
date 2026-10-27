from dataclasses import dataclass
from typing import Literal

from whichsdk.detect.deps import DependencyChanges
from whichsdk.detect.transcript import Switch, TranscriptFindings

Verdict = Literal["picked", "installed_unused", "multiple", "other", "no_sdk", "undetermined"]
Reason = Literal[
    "run_failed",
    "max_turns",
    "package_json_unreadable",
    "competitor_import_without_install",
    "competitor_declared_without_lock",
    "no_code_added",
    "lockfile_changed_without_package",
    "install_command_changed_nothing",
]


@dataclass(frozen=True)
class Detection:
    """What the detectors found in one attempt, and the verdict drawn from it.

    reason says why the verdict is undetermined; it is None for every other verdict.
    """

    verdict: Verdict
    picked: str | None
    reason: Reason | None
    added: dict[str, str | None]
    removed: dict[str, str | None]
    imported: dict[str, str]
    switches: list[Switch]
    searched: bool


def judge(
    changes: DependencyChanges | None,
    imported: dict[str, str],
    wrote_code: bool,
    findings: TranscriptFindings,
    finished: bool,
) -> Detection:
    """Draw the verdict; whatever the findings do not positively show is undetermined."""
    verdict, picked, reason = _verdict(changes, imported, wrote_code, findings, finished)
    added = {} if changes is None else changes.added
    removed = {} if changes is None else changes.removed
    return Detection(
        verdict, picked, reason, added, removed, imported, findings.switches, findings.searched
    )


def _verdict(
    changes: DependencyChanges | None,
    imported: dict[str, str],
    wrote_code: bool,
    findings: TranscriptFindings,
    finished: bool,
) -> tuple[Verdict, str | None, Reason | None]:
    if not finished:
        return "undetermined", None, "max_turns" if findings.hit_turn_limit else "run_failed"
    if changes is None:
        return "undetermined", None, "package_json_unreadable"
    installed = sorted({competitor for competitor in changes.added.values() if competitor})
    if len(installed) > 1:
        return "multiple", None, None
    if installed:
        if installed[0] in imported.values():
            return "picked", installed[0], None
        return "installed_unused", None, None
    if imported:
        return "undetermined", None, "competitor_import_without_install"
    if any(changes.unlocked.values()):
        return "undetermined", None, "competitor_declared_without_lock"
    if changes.added:
        return "other", None, None
    if not wrote_code:
        return "undetermined", None, "no_code_added"
    if changes.package_files_changed:
        return "undetermined", None, "lockfile_changed_without_package"
    if findings.ran_install:
        return "undetermined", None, "install_command_changed_nothing"
    return "no_sdk", None, None
