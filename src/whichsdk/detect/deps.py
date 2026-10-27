import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")


@dataclass(frozen=True)
class DependencyChanges:
    """Packages installed, removed or declared without a lock, each with its competitor or None.

    package_files_changed is True when any package file differs byte for byte.
    """

    added: dict[str, str | None]
    removed: dict[str, str | None]
    unlocked: dict[str, str | None]
    package_files_changed: bool


@dataclass(frozen=True)
class _Packages:
    declared: set[str]
    installed: set[str]


def package_name(spec: str) -> str:
    """Return the package name from a name or a name@version spec, scoped names included."""
    end = spec.find("@", 1)
    return spec if end == -1 else spec[:end]


def compare_dependencies(
    baseline: Path, final: Path, owners: Mapping[str, str], package_files: Iterable[str]
) -> DependencyChanges | None:
    """Compare the package files of two directories; None if a package.json cannot be read."""
    before, after = _read_packages(baseline), _read_packages(final)
    if before is None or after is None:
        return None
    return DependencyChanges(
        added=_owned(after.installed - before.installed, owners),
        removed=_owned(before.installed - after.installed, owners),
        unlocked=_owned(after.declared - before.declared - after.installed, owners),
        package_files_changed=any(
            _contents(baseline / name) != _contents(final / name) for name in package_files
        ),
    )


def _read_packages(directory: Path) -> _Packages | None:
    try:
        package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    declared = {name for section in DEPENDENCY_SECTIONS for name in package.get(section, {})}
    locked = _locked(directory)
    return _Packages(declared, declared if locked is None else declared & locked)


def _owned(names: Iterable[str], owners: Mapping[str, str]) -> dict[str, str | None]:
    return {name: owners.get(name) for name in sorted(names)}


def _contents(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _locked(directory: Path) -> set[str] | None:
    names: set[str] | None = None
    for lockfile, read_names in LOCKFILE_READERS.items():
        try:
            found = read_names(directory / lockfile)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        names = (names or set()) | found
    return names


def _npm_names(path: Path) -> set[str]:
    packages = json.loads(path.read_text(encoding="utf-8")).get("packages", {})
    return {key.rpartition("node_modules/")[2] for key in packages if "node_modules/" in key}


def _pnpm_names(path: Path) -> set[str]:
    packages = yaml.safe_load(path.read_text(encoding="utf-8")).get("packages", {})
    return {package_name(key) for key in packages}


def _yarn_names(path: Path) -> set[str]:
    headers = [
        line[:-1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.endswith(":") and not line.startswith((" ", "#"))
    ]
    return {package_name(entry.strip('"')) for header in headers for entry in header.split(", ")}


LOCKFILE_READERS: dict[str, Callable[[Path], set[str]]] = {
    "package-lock.json": _npm_names,
    "pnpm-lock.yaml": _pnpm_names,
    "yarn.lock": _yarn_names,
}
