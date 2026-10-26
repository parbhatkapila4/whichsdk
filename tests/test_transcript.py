import json
from pathlib import Path

import pytest

from whichsdk.detect.transcript import Switch, TranscriptFindings, read_transcript

FIXTURES = Path(__file__).parent / "fixtures"
OWNERS = {
    "next-auth": "Auth.js",
    "better-auth": "Better Auth",
    "@clerk/nextjs": "Clerk",
    "@clerk/backend": "Clerk",
    "@supabase/ssr": "Supabase Auth",
    "next": "Next.js",
}
TO_BETTER_AUTH = [Switch("Auth.js", "Better Auth")]


def tool_use(call_id: str, command: str, name: str = "Bash") -> str:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": {"command": command}}
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [block]}})


def tool_result(call_id: str, is_error: bool = False) -> str:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": "x", "is_error": is_error}
    return json.dumps({"type": "user", "message": {"role": "user", "content": [block]}})


def read_lines(tmp_path: Path, *lines: str) -> TranscriptFindings:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read_transcript(path, OWNERS)


def read_commands(tmp_path: Path, *commands: str) -> TranscriptFindings:
    lines: list[str] = []
    for number, command in enumerate(commands):
        lines += [tool_use(f"call_{number}", command), tool_result(f"call_{number}")]
    return read_lines(tmp_path, *lines)


def test_an_install_then_an_uninstall_then_another_install_is_a_switch(tmp_path: Path) -> None:
    findings = read_commands(
        tmp_path, "npm install next-auth@beta", "npm uninstall next-auth", "npm install better-auth"
    )

    assert findings.switches == TO_BETTER_AUTH


def test_installing_the_new_competitor_before_removing_the_old_one_is_a_switch(
    tmp_path: Path,
) -> None:
    findings = read_commands(
        tmp_path, "npm install next-auth", "npm install better-auth && npm uninstall next-auth"
    )

    assert findings.switches == TO_BETTER_AUTH


def test_switches_are_reported_in_order(tmp_path: Path) -> None:
    findings = read_commands(
        tmp_path,
        "npm i next-auth",
        "npm rm next-auth",
        "npm i better-auth",
        "npm uninstall better-auth",
        "npm i @clerk/nextjs",
    )

    assert findings.switches == [Switch("Auth.js", "Better Auth"), Switch("Better Auth", "Clerk")]


@pytest.mark.parametrize(
    "commands",
    [
        ["npm i better-auth"],
        ["npm i next-auth", "npm rm next-auth", "npm i next-auth@latest"],
        ["npm i @clerk/nextjs @clerk/backend", "npm rm @clerk/backend", "pnpm add @clerk/backend"],
        ["npm rm next-auth", "npm i better-auth"],
        ["npm i next-auth", "npm i better-auth"],
    ],
)
def test_these_sequences_are_not_switches(tmp_path: Path, commands: list[str]) -> None:
    assert read_commands(tmp_path, *commands).switches == []


@pytest.mark.parametrize(
    "commands",
    [
        ["pnpm add next-auth", "pnpm remove next-auth", "pnpm add better-auth"],
        ["yarn add next-auth", "yarn remove next-auth", "yarn add better-auth"],
        ["npm i -D next-auth", "npm rm next-auth", "npm add --save better-auth"],
        ["npm install next-auth", "pnpm uninstall next-auth", "yarn add better-auth"],
        ["pnpm i next-auth", "pnpm un next-auth", "pnpm install better-auth"],
    ],
)
def test_npm_pnpm_and_yarn_commands_are_all_understood(tmp_path: Path, commands: list[str]) -> None:
    assert read_commands(tmp_path, *commands).switches == TO_BETTER_AUTH


def test_commands_are_found_inside_chains_pipes_and_subshells(tmp_path: Path) -> None:
    findings = read_commands(
        tmp_path,
        "cd /workspace && npm install --legacy-peer-deps next-auth@5.0.0-beta.25 2>&1 | tail -5",
        "(cd app; pnpm remove next-auth)",
        "CI=1 timeout 120 yarn add -D better-auth@latest zod",
    )

    assert findings.switches == TO_BETTER_AUTH


def test_scoped_packages_are_understood(tmp_path: Path) -> None:
    findings = read_commands(
        tmp_path, "npm i @clerk/nextjs@latest", "npm rm @clerk/nextjs", 'npm i "@supabase/ssr"'
    )

    assert findings.switches == [Switch("Clerk", "Supabase Auth")]


def test_line_continuations_are_joined(tmp_path: Path) -> None:
    findings = read_commands(
        tmp_path, "npm i next-auth", "npm rm next-auth", "npm install \\\n  zod \\\n  better-auth"
    )

    assert findings.switches == TO_BETTER_AUTH


@pytest.mark.parametrize(
    "lookalike",
    [
        'echo "npm install better-auth"',
        "echo 'pnpm add better-auth'",
        "# npm install better-auth",
        "cat <<'EOF' > notes.md\nnpm install better-auth\nEOF",
        "npm install",
        "npm ci",
        "npm run install-better-auth",
        "npm ls better-auth",
        "npm view better-auth",
        "npx better-auth init",
        "pnpm dlx better-auth init",
        "grep -r better-auth . && yarn",
    ],
)
def test_text_that_only_looks_like_an_install_is_ignored(tmp_path: Path, lookalike: str) -> None:
    findings = read_commands(tmp_path, "npm i next-auth", "npm rm next-auth", lookalike)

    assert findings.switches == []


def test_a_here_document_hides_its_text_but_not_the_commands_around_it(tmp_path: Path) -> None:
    notes = "cat > notes.md <<'EOF'\nnpm install @clerk/nextjs\nEOF\nnpm install next-auth"

    findings = read_commands(tmp_path, notes, "npm uninstall next-auth", "npm install better-auth")

    assert findings.switches == TO_BETTER_AUTH


def test_an_unbalanced_quote_on_one_line_does_not_hide_the_next_line(tmp_path: Path) -> None:
    findings = read_commands(
        tmp_path,
        "echo don't stop\nnpm install next-auth",
        "npm uninstall next-auth",
        "npm install better-auth",
    )

    assert findings.switches == TO_BETTER_AUTH


def test_a_failed_install_does_not_count(tmp_path: Path) -> None:
    lines = [
        tool_use("a", "npm i next-auth"),
        tool_result("a"),
        tool_use("b", "npm rm next-auth"),
        tool_result("b"),
        tool_use("c", "npm i better-auth"),
        tool_result("c", is_error=True),
    ]

    assert read_lines(tmp_path, *lines).switches == []
    assert (
        read_lines(tmp_path, *lines, tool_use("d", "npm i better-auth"), tool_result("d")).switches
        == TO_BETTER_AUTH
    )


def test_a_call_that_never_got_a_result_does_not_count(tmp_path: Path) -> None:
    lines = [
        tool_use("a", "npm i next-auth"),
        tool_result("a"),
        tool_use("b", "npm rm next-auth"),
        tool_result("b"),
        tool_use("c", "npm i better-auth"),
    ]

    assert read_lines(tmp_path, *lines).switches == []


@pytest.mark.parametrize(
    ("tool", "searched"), [("WebSearch", True), ("WebFetch", True), ("Read", False)]
)
def test_web_tools_are_noticed(tmp_path: Path, tool: str, searched: bool) -> None:
    findings = read_lines(tmp_path, tool_use("a", "x", name=tool))

    assert findings.searched is searched


def test_a_web_lookup_counts_even_when_it_failed(tmp_path: Path) -> None:
    findings = read_lines(tmp_path, tool_use("a", "x", name="WebFetch"), tool_result("a", True))

    assert findings.searched is True


def test_curl_in_a_shell_command_is_not_a_web_tool(tmp_path: Path) -> None:
    assert read_commands(tmp_path, "curl -s https://example.com").searched is False


def test_lines_that_are_not_events_are_skipped(tmp_path: Path) -> None:
    findings = read_lines(
        tmp_path,
        "timeout: sending signal TERM",
        "",
        "42",
        "[1, 2]",
        tool_use("a", "npm i next-auth"),
        tool_result("a"),
        "{not json",
        tool_use("b", "npm rm next-auth"),
        tool_result("b"),
        tool_use("c", "npm i better-auth"),
        tool_result("c"),
    )

    assert findings.switches == TO_BETTER_AUTH


def test_the_real_transcript_has_no_installs_and_no_web_lookups(tmp_path: Path) -> None:
    findings = read_transcript(FIXTURES / "real" / "transcript.jsonl", OWNERS)

    assert findings == TranscriptFindings([], False)


def test_the_switch_run_shows_one_switch_a_failed_install_and_a_search() -> None:
    findings = read_transcript(FIXTURES / "switch" / "transcript.jsonl", OWNERS)

    assert findings == TranscriptFindings(TO_BETTER_AUTH, True)
