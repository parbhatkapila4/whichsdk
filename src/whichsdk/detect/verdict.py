from dataclasses import dataclass
from typing import Literal

from whichsdk.detect.deps import DependencyChanges
from whichsdk.detect.transcript import Switch, TranscriptFindings

Verdict = Literal["picked", "installed_unused", "multiple", "undetermined"]


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
    changes: DependencyChanges, imported: dict[str, str], findings: TranscriptFindings
) -> Detection:
    """Draw the verdict; whatever the findings cannot show is undetermined, never none."""
    installed = sorted({competitor for competitor in changes.added.values() if competitor})
    verdict: Verdict = "undetermined"
    picked = None
    if len(installed) > 1:
        verdict = "multiple"
    elif installed and installed[0] in imported.values():
        verdict, picked = "picked", installed[0]
    elif installed:
        verdict = "installed_unused"
    return Detection(
        verdict,
        picked,
        changes.added,
        changes.removed,
        imported,
        findings.switches,
        findings.searched,
    )
