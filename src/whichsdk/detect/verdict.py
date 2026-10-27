from dataclasses import dataclass
from typing import Literal

from whichsdk.detect.deps import DependencyChanges
from whichsdk.detect.transcript import Switch, TranscriptFindings

Verdict = Literal["picked", "installed_unused", "multiple", "other", "no_sdk", "undetermined"]


@dataclass(frozen=True)
class Detection:
    """What the detectors found in one attempt, and the verdict drawn from it."""

    verdict: Verdict
    picked: str | None
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
) -> Detection:
    """Draw the verdict; whatever the findings do not positively show is undetermined."""
    verdict, picked = _verdict(changes, imported, wrote_code, findings)
    added = {} if changes is None else changes.added
    removed = {} if changes is None else changes.removed
    return Detection(
        verdict, picked, added, removed, imported, findings.switches, findings.searched
    )


def _verdict(
    changes: DependencyChanges | None,
    imported: dict[str, str],
    wrote_code: bool,
    findings: TranscriptFindings,
) -> tuple[Verdict, str | None]:
    if changes is None:
        return "undetermined", None
    installed = sorted({competitor for competitor in changes.added.values() if competitor})
    if len(installed) > 1:
        return "multiple", None
    if installed:
        if installed[0] in imported.values():
            return "picked", installed[0]
        return "installed_unused", None
    # A competitor that is imported or declared but not installed contradicts both labels below.
    if imported or any(changes.unlocked.values()):
        return "undetermined", None
    if changes.added:
        return "other", None
    # A run that wrote no code says nothing about how the agent builds auth.
    if wrote_code and not changes.package_files_changed and not findings.ran_install:
        return "no_sdk", None
    return "undetermined", None
