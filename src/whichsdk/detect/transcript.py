import json
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whichsdk.detect.deps import is_helper, package_name

MANAGERS = {"npm", "pnpm", "yarn"}
INSTALL_VERBS = {"install", "i", "add"}
UNINSTALL_VERBS = {"uninstall", "remove", "rm", "un"}
WEB_TOOLS = {"WebSearch", "WebFetch"}
TURN_LIMIT_SUBTYPE = "error_max_turns"
OPERATOR_CHARACTERS = set("();<>|&")
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*['\"]?(\w+)['\"]?")


@dataclass(frozen=True)
class Switch:
    """A competitor the agent installed and then replaced with another."""

    from_competitor: str
    to_competitor: str


@dataclass(frozen=True)
class TranscriptFindings:
    """What a transcript shows: switches, web lookups, package installs and a turn limit.

    ran_install is True when an install command named a package that is not a helper.
    """

    switches: list[Switch]
    searched: bool
    ran_install: bool
    hit_turn_limit: bool


@dataclass(frozen=True)
class _PackageEvent:
    action: str
    package: str
    competitor: str | None


def read_transcript(
    path: Path, owners: Mapping[str, str], helpers: Sequence[str] = ()
) -> TranscriptFindings:
    """Find competitor switches, package installs, web lookups and a turn limit in a transcript."""
    events = list(_events(path))
    succeeded = {
        block["tool_use_id"]: not block.get("is_error") for block in _blocks(events, "tool_result")
    }
    calls = list(_blocks(events, "tool_use"))
    package_events = [
        (call["id"], package_event)
        for call in calls
        if call.get("name") == "Bash"
        for segment in _commands(call.get("input", {}).get("command", ""))
        for package_event in _package_events(segment, owners)
    ]
    switches = _switches(
        event for call_id, event in package_events if succeeded.get(call_id, False)
    )
    searched = any(call.get("name") in WEB_TOOLS for call in calls)
    ran_install = any(
        event.action == "install"
        and (event.competitor is not None or not is_helper(event.package, helpers))
        for _, event in package_events
    )
    hit_turn_limit = any(
        event.get("type") == "result" and event.get("subtype") == TURN_LIMIT_SUBTYPE
        for event in events
    )
    return TranscriptFindings(switches, searched, ran_install, hit_turn_limit)


def _events(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _blocks(events: Iterable[dict[str, Any]], kind: str) -> Iterator[dict[str, Any]]:
    for event in events:
        content = event.get("message", {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == kind:
                    yield block


def _commands(command: str) -> Iterator[list[str]]:
    for line in _without_heredocs(command):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            continue
        segment: list[str] = []
        for token in tokens:
            if set(token) <= OPERATOR_CHARACTERS:
                # The 2 in 2>&1 is a file descriptor, not an argument of the command.
                if segment and segment[-1].isdigit():
                    segment.pop()
                if segment:
                    yield segment
                segment = []
            else:
                segment.append(token)
        if segment:
            yield segment


def _without_heredocs(command: str) -> list[str]:
    lines: list[str] = []
    delimiter: str | None = None
    for line in command.replace("\\\n", " ").splitlines():
        if delimiter is not None:
            delimiter = None if line.strip() == delimiter else delimiter
            continue
        heredoc = HEREDOC.search(line)
        delimiter = heredoc[1] if heredoc else None
        lines.append(line)
    return lines


def _package_events(segment: list[str], owners: Mapping[str, str]) -> Iterator[_PackageEvent]:
    manager = next((i for i, word in enumerate(segment) if word in MANAGERS), None)
    if manager is None:
        return
    words = [word for word in segment[manager + 1 :] if not word.startswith("-")]
    if not words or words[0] not in INSTALL_VERBS | UNINSTALL_VERBS:
        return
    action = "install" if words[0] in INSTALL_VERBS else "uninstall"
    for spec in words[1:]:
        package = package_name(spec)
        yield _PackageEvent(action, package, owners.get(package))


def _switches(events: Iterable[_PackageEvent]) -> list[Switch]:
    installed: dict[str, set[str]] = {}
    left: str | None = None
    switches: list[Switch] = []
    for event in events:
        if event.competitor is None:
            continue
        if event.action == "install":
            if event.competitor not in installed:
                if left is not None and left != event.competitor:
                    switches.append(Switch(left, event.competitor))
                left = None
            installed.setdefault(event.competitor, set()).add(event.package)
        elif event.competitor in installed:
            installed[event.competitor].discard(event.package)
            if not installed[event.competitor]:
                del installed[event.competitor]
                if installed:
                    switches.append(Switch(event.competitor, next(reversed(installed))))
                else:
                    left = event.competitor
    return switches
