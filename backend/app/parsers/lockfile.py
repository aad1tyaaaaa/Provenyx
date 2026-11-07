from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote


DEPENDENCY_FIELDS = {
    "dependencies": "prod",
    "devDependencies": "dev",
    "optionalDependencies": "optional",
    "peerDependencies": "peer",
}


@dataclass(frozen=True)
class ParsedPackage:
    name: str
    version: str
    lockfile_path: str
    resolved: str | None = None
    integrity: str | None = None
    dependency_groups: dict[str, dict[str, str]] = field(default_factory=dict)
    # npm records `hasInstallScript: true` for packages with install lifecycle scripts (lockfile v2+).
    has_install_script: bool = False


@dataclass(frozen=True)
class ParsedLockfile:
    name: str | None
    version: str | None
    lockfile_version: int
    ecosystem: str
    package_manager: str
    component_path: str
    root_dependency_groups: dict[str, dict[str, str]]
    packages: tuple[ParsedPackage, ...]
    warnings: tuple[str, ...] = ()


def parse_lockfile(lockfile: Mapping[str, Any]) -> ParsedLockfile:
    """Normalize validated npm package-lock data without executing package code."""
    lockfile_version = int(lockfile["lockfileVersion"])
    if lockfile_version >= 2:
        return _parse_modern_lockfile(lockfile, lockfile_version)
    return _parse_v1_lockfile(lockfile)


def _parse_modern_lockfile(lockfile: Mapping[str, Any], lockfile_version: int) -> ParsedLockfile:
    package_entries = lockfile.get("packages")
    if not isinstance(package_entries, Mapping):
        raise ValueError("lockfileVersion 2/3 requires a packages object")

    root = package_entries.get("")
    root_entry = root if isinstance(root, Mapping) else {}
    packages: list[ParsedPackage] = []
    raw_warnings = lockfile.get("provenyxWarnings")
    warnings = [warning for warning in raw_warnings if isinstance(warning, str)] if isinstance(raw_warnings, list) else []

    for raw_path, raw_entry in package_entries.items():
        if not raw_path or not isinstance(raw_path, str) or not isinstance(raw_entry, Mapping):
            continue
        name = _package_name(raw_path, raw_entry)
        version = raw_entry.get("version")
        if not name or not isinstance(version, str) or not version:
            warnings.append(f"Skipped package at {raw_path!r}: name or exact version is unavailable")
            continue
        packages.append(
            ParsedPackage(
                name=name,
                version=version,
                lockfile_path=raw_path,
                resolved=_optional_string(raw_entry.get("resolved")),
                integrity=_optional_string(raw_entry.get("integrity")),
                dependency_groups=_dependency_groups(raw_entry),
                has_install_script=_has_install_script(raw_entry),
            )
        )

    ecosystem, package_manager, component_path = _source_identity(lockfile)
    return ParsedLockfile(
        name=_optional_string(lockfile.get("name")) or _optional_string(root_entry.get("name")),
        version=_optional_string(lockfile.get("version")) or _optional_string(root_entry.get("version")),
        lockfile_version=lockfile_version,
        ecosystem=ecosystem,
        package_manager=package_manager,
        component_path=component_path,
        root_dependency_groups=_dependency_groups(root_entry),
        packages=tuple(packages),
        warnings=tuple(warnings),
    )


def _parse_v1_lockfile(lockfile: Mapping[str, Any]) -> ParsedLockfile:
    dependencies = lockfile.get("dependencies")
    if not isinstance(dependencies, Mapping):
        raise ValueError("lockfileVersion 1 requires a dependencies object")

    packages: list[ParsedPackage] = []
    warnings: list[str] = []
    _walk_v1_dependencies(dependencies, "", packages, warnings)
    root_dependencies = {
        str(name): str(entry.get("version", ""))
        for name, entry in dependencies.items()
        if isinstance(name, str) and isinstance(entry, Mapping)
    }
    return ParsedLockfile(
        name=_optional_string(lockfile.get("name")),
        version=_optional_string(lockfile.get("version")),
        lockfile_version=1,
        ecosystem="npm",
        package_manager="npm",
        component_path=_component_path(lockfile.get("provenyxComponentPath")),
        root_dependency_groups={"prod": root_dependencies},
        packages=tuple(packages),
        warnings=tuple(warnings),
    )


def _walk_v1_dependencies(
    dependencies: Mapping[str, Any],
    parent_path: str,
    packages: list[ParsedPackage],
    warnings: list[str],
) -> None:
    for name, raw_entry in dependencies.items():
        if not isinstance(name, str) or not isinstance(raw_entry, Mapping):
            continue
        path = f"{parent_path}/node_modules/{name}" if parent_path else f"node_modules/{name}"
        version = raw_entry.get("version")
        if not isinstance(version, str) or not version:
            warnings.append(f"Skipped package at {path!r}: exact version is unavailable")
            continue
        requires = raw_entry.get("requires")
        dependency_groups = {
            "prod": {
                str(dependency_name): str(specifier)
                for dependency_name, specifier in requires.items()
                if isinstance(dependency_name, str) and isinstance(specifier, str)
            }
        } if isinstance(requires, Mapping) else {}
        packages.append(
            ParsedPackage(
                name=name,
                version=version,
                lockfile_path=path,
                resolved=_optional_string(raw_entry.get("resolved")),
                integrity=_optional_string(raw_entry.get("integrity")),
                dependency_groups=dependency_groups,
            )
        )
        nested = raw_entry.get("dependencies")
        if isinstance(nested, Mapping):
            _walk_v1_dependencies(nested, path, packages, warnings)


def _dependency_groups(entry: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    groups: dict[str, dict[str, str]] = {}
    for field_name, dependency_type in DEPENDENCY_FIELDS.items():
        values = entry.get(field_name)
        if not isinstance(values, Mapping):
            continue
        groups[dependency_type] = {
            str(name): str(specifier)
            for name, specifier in values.items()
            if isinstance(name, str) and isinstance(specifier, str)
        }
    return groups


def _package_name(path: str, entry: Mapping[str, Any]) -> str | None:
    explicit_name = _optional_string(entry.get("name"))
    if explicit_name:
        return explicit_name
    marker = "node_modules/"
    if marker not in path:
        return None
    return path.rsplit(marker, 1)[1] or None


def _has_install_script(entry: Mapping[str, Any]) -> bool:
    if entry.get("hasInstallScript") is True:
        return True
    scripts = entry.get("scripts")
    return isinstance(scripts, Mapping) and any(scripts.get(name) for name in ("preinstall", "install", "postinstall"))


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _source_identity(lockfile: Mapping[str, Any]) -> tuple[str, str, str]:
    source = lockfile.get("provenyxSource")
    defaults = {
        "composer.lock": ("Packagist", "composer"),
        "pnpm-lock.yaml": ("npm", "pnpm"),
    }
    default_ecosystem, default_manager = defaults.get(source, ("npm", "npm"))
    ecosystem = _optional_string(lockfile.get("provenyxEcosystem")) or default_ecosystem
    package_manager = _optional_string(lockfile.get("provenyxPackageManager")) or default_manager
    return ecosystem, package_manager, _component_path(lockfile.get("provenyxComponentPath"))


def _component_path(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        return "."
    parts = value.replace("\\", "/").split("/")
    return value if all(part not in ("", ".", "..") for part in parts) else "."


def package_purl(ecosystem: str, name: str, version: str) -> str:
    purl_type = {
        "npm": "npm",
        "Packagist": "composer",
        "PyPI": "pypi",
        "Go": "golang",
        "crates.io": "cargo",
        "Maven": "maven",
        "NuGet": "nuget",
        "RubyGems": "gem",
        "Pub": "pub",
    }.get(ecosystem, ecosystem.lower().replace(" ", "-"))
    encoded_name = "/".join(quote(part, safe=".-_~") for part in name.split("/"))
    return f"pkg:{purl_type}/{encoded_name}@{quote(version, safe='.-_~+')}"