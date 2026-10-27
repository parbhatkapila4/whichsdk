import re
from collections.abc import Iterator, Mapping
from pathlib import Path

SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}
MODULE = re.compile(r"""(?:\bfrom|\bimport|\brequire)\s*\(?\s*['"]([^'"]+)['"]""")
PACKAGE = re.compile(r"(?:@[\w.-]+/)?[\w.-]+")


def find_imports(diff_path: Path, owners: Mapping[str, str]) -> dict[str, str]:
    """Find the competitor packages that source lines added in the diff import or require."""
    imported: dict[str, str] = {}
    for line in _added_source_lines(diff_path):
        for module in MODULE.findall(line):
            package = PACKAGE.match(module)
            if package and package[0] in owners:
                imported[package[0]] = owners[package[0]]
    return dict(sorted(imported.items()))


def adds_source(diff_path: Path) -> bool:
    """Tell whether the diff adds any code to a JavaScript or TypeScript file."""
    return any(line.strip() for line in _added_source_lines(diff_path))


def _added_source_lines(diff_path: Path) -> Iterator[str]:
    suffix = ""
    for line in diff_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("+++ "):
            suffix = Path(line[4:]).suffix
        elif line.startswith("+") and suffix in SOURCE_SUFFIXES and not _is_comment(line[1:]):
            yield line[1:]


def _is_comment(code: str) -> bool:
    return code.lstrip().startswith(("//", "/*", "*"))
