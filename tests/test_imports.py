from pathlib import Path

import pytest

from whichsdk.detect.imports import adds_source, find_imports

FIXTURES = Path(__file__).parent / "fixtures"
OWNERS = {
    "@clerk/nextjs": "Clerk",
    "better-auth": "Better Auth",
    "next-auth": "Auth.js",
    "@supabase/ssr": "Supabase Auth",
}


def diff_of(tmp_path: Path, filename: str, *added: str) -> Path:
    lines = [f"diff --git a/{filename} b/{filename}", f"+++ b/{filename}", "@@ -0,0 +1 @@"]
    path = tmp_path / "changes.diff"
    path.write_text("\n".join([*lines, *added]) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("added", "package"),
    [
        (["import { auth } from '@clerk/nextjs/server';"], "@clerk/nextjs"),
        (['const auth = require("better-auth");'], "better-auth"),
        (["const react = await import('next-auth/react');"], "next-auth"),
        (["import '@supabase/ssr';"], "@supabase/ssr"),
        (["import {", "  GitHub,", '} from "next-auth/providers/github";'], "next-auth"),
        (["export * from 'better-auth/client';"], "better-auth"),
        (["import type { Session } from 'next-auth';"], "next-auth"),
        (["import auth = require('better-auth');"], "better-auth"),
    ],
)
def test_every_way_of_importing_a_package_is_found(
    tmp_path: Path, added: list[str], package: str
) -> None:
    path = diff_of(tmp_path, "lib/auth.ts", *(f"+{line}" for line in added))

    assert find_imports(path, OWNERS) == {package: OWNERS[package]}


def test_only_added_lines_of_source_files_count(tmp_path: Path) -> None:
    path = tmp_path / "changes.diff"
    path.write_text(
        "\n".join(
            [
                "+++ b/lib/auth.ts",
                '-import { auth } from "@clerk/nextjs";',
                ' import { betterAuth } from "better-auth";',
                '+// import { signIn } from "next-auth"',
                '+ * import { x } from "@supabase/ssr"',
                "+++ b/README.md",
                '+import { auth } from "@clerk/nextjs";',
                "+++ b/package.json",
                '+    "better-auth": "^1.3.4",',
                "+++ /dev/null",
                '-import { auth } from "next-auth";',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert find_imports(path, OWNERS) == {}


def test_lookalike_packages_and_path_aliases_are_not_matched(tmp_path: Path) -> None:
    path = diff_of(
        tmp_path,
        "app/page.tsx",
        '+import { Button } from "better-auth-ui";',
        '+import { auth } from "@/lib/better-auth";',
        '+import { auth } from "./better-auth";',
        '+import { auth } from "better-authx/client";',
        '+import { auth } from "node:fs";',
    )

    assert find_imports(path, OWNERS) == {}


def test_each_source_file_extension_is_scanned(tmp_path: Path) -> None:
    for extension in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts"):
        path = diff_of(tmp_path, f"x.{extension}", '+import a from "better-auth";')

        assert find_imports(path, OWNERS) == {"better-auth": "Better Auth"}, extension


def test_an_empty_diff_imports_nothing(tmp_path: Path) -> None:
    path = tmp_path / "changes.diff"
    path.write_text("", encoding="utf-8")

    assert find_imports(path, OWNERS) == {}


def test_the_real_diff_is_scanned_by_package_not_by_subpath() -> None:
    owners = {"next": "Next.js", "react": "React", "server-only": "Server Only", "zod": "Zod"}

    imported = find_imports(FIXTURES / "real" / "changes.diff", owners)

    assert imported == {"next": "Next.js", "react": "React", "server-only": "Server Only"}


def test_the_real_diff_imports_no_competitor() -> None:
    assert find_imports(FIXTURES / "real" / "changes.diff", OWNERS) == {}


@pytest.mark.parametrize("extension", ["ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts"])
def test_code_added_to_any_kind_of_source_file_counts(tmp_path: Path, extension: str) -> None:
    path = diff_of(tmp_path, f"lib/session.{extension}", "+export const maxAge = 60;")

    assert adds_source(path) is True


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["-export const removed = 1;", " export const kept = 2;"],
        ["+// only a comment", "+", "+    ", "+ * a doc line", "+/* a block */"],
    ],
    ids=["empty", "nothing added", "comments and blank lines"],
)
def test_nothing_that_is_code_added_to_a_source_file_does_not_count(
    tmp_path: Path, lines: list[str]
) -> None:
    assert adds_source(diff_of(tmp_path, "lib/session.ts", *lines)) is False


def test_code_added_outside_source_files_does_not_count(tmp_path: Path) -> None:
    path = tmp_path / "changes.diff"
    path.write_text(
        "\n".join(
            [
                "+++ b/README.md",
                "+Notes on the login flow.",
                "+++ b/.env.local",
                "+SESSION_SECRET=change-me",
                "+++ b/app/globals.css",
                "+body { margin: 0; }",
                "+++ /dev/null",
                "-export const gone = 1;",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert adds_source(path) is False


def test_the_real_diff_and_the_switch_diff_add_source() -> None:
    assert adds_source(FIXTURES / "real" / "changes.diff") is True
    assert adds_source(FIXTURES / "switch" / "changes.diff") is True
