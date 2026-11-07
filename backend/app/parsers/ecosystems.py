from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import yaml

from app.parsers.composer import normalize_composer_lockfile

SUPPORTED_DEPENDENCY_FILES = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "composer.lock",
    "Pipfile.lock",
    "requirements.txt",
    "go.sum",
    "Cargo.lock",
    "pom.xml",
    "gradle.lockfile",
    "packages.lock.json",
    "Gemfile.lock",
    "pubspec.lock",
    "Package.resolved",
)

AUXILIARY_FILES: dict[str, tuple[str, ...]] = {
    "yarn.lock": ("package.json",),
    "composer.lock": ("composer.json",),
    "go.sum": ("go.mod",),
}


def normalize_dependency_file(
    filename: str,
    content: str | bytes,
    auxiliaries: dict[str, str | bytes] | None = None,
) -> dict[str, Any]:
    name = PurePosixPath(filename).name
    if name not in SUPPORTED_DEPENDENCY_FILES:
        raise ValueError(f"Unsupported dependency file: {name}")
    text = _text(content)
    auxiliary = auxiliaries or {}
    parsers = {
        "package-lock.json": lambda: _npm(text),
        "pnpm-lock.yaml": lambda: _pnpm(text),
        "yarn.lock": lambda: _yarn(text, _optional_text(auxiliary.get("package.json"))),
        "bun.lock": lambda: _bun(text),
        "composer.lock": lambda: normalize_composer_lockfile(text, auxiliary.get("composer.json")),
        "Pipfile.lock": lambda: _pipfile(text),
        "requirements.txt": lambda: _requirements(text),
        "go.sum": lambda: _go_sum(text, _optional_text(auxiliary.get("go.mod"))),
        "Cargo.lock": lambda: _cargo(text),
        "pom.xml": lambda: _maven(text),
        "gradle.lockfile": lambda: _gradle(text),
        "packages.lock.json": lambda: _nuget(text),
        "Gemfile.lock": lambda: _gemfile(text),
        "pubspec.lock": lambda: _pub(text),
        "Package.resolved": lambda: _swift(text),
    }
    result = parsers[name]()
    if not isinstance(result, dict):
        raise ValueError(f"{name} did not produce a dependency snapshot")
    return result


def _snapshot(
    ecosystem: str,
    manager: str,
    source: str,
    packages: list[dict[str, Any]],
    roots: dict[str, str] | None = None,
    dev_roots: dict[str, str] | None = None,
    warnings: list[str] | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    entries: dict[str, Any] = {"": {"dependencies": roots or {}, "devDependencies": dev_roots or {}}}
    for index, package in enumerate(packages):
        name = str(package["name"])
        version = str(package["version"])
        entries[f"packages/{quote(name, safe='')}@{quote(version, safe='')}#{index}"] = {
            "name": name,
            "version": version,
            "dependencies": package.get("dependencies", {}),
            **({"integrity": package["integrity"]} if package.get("integrity") else {}),
        }
    return {
        "name": project_name,
        "lockfileVersion": 3,
        "packages": entries,
        "provenyxSource": source,
        "provenyxEcosystem": ecosystem,
        "provenyxPackageManager": manager,
        "provenyxWarnings": warnings or [],
    }


def _npm(text: str) -> dict[str, Any]:
    data = _json_object(text, "package-lock.json")
    if data.get("lockfileVersion") not in (1, 2, 3):
        raise ValueError("package-lock.json has an unsupported lockfileVersion")
    return data


def _pnpm(text: str) -> dict[str, Any]:
    data = _yaml_object(text, "pnpm-lock.yaml")
    raw_packages = data.get("packages") if isinstance(data.get("packages"), dict) else {}
    records: dict[str, dict[str, Any]] = {}
    for key, raw in raw_packages.items():
        if not isinstance(raw, dict):
            continue
        identity = _pnpm_identity(str(key), raw)
        if identity:
            package_name, version = identity
            records[f"{package_name}@{version}"] = {
                "name": package_name,
                "version": version,
                "integrity": (raw.get("resolution") or {}).get("integrity") if isinstance(raw.get("resolution"), dict) else None,
                "raw_dependencies": {**_dict(raw.get("dependencies")), **_dict(raw.get("optionalDependencies"))},
            }
    warnings: list[str] = []
    _resolve_dependencies(records, warnings, "pnpm")
    importer = (_dict(data.get("importers"))).get(".", {})
    importer = importer if isinstance(importer, dict) else {}
    roots = _resolve_roots({**_dict(importer.get("dependencies")), **_dict(importer.get("optionalDependencies"))}, records, warnings, "pnpm")
    dev = _resolve_roots(_dict(importer.get("devDependencies")), records, warnings, "pnpm")
    return _snapshot("npm", "pnpm", "pnpm-lock.yaml", list(records.values()), roots, dev, warnings)


def _pnpm_identity(key: str, raw: dict[str, Any]) -> tuple[str, str] | None:
    clean = key.lstrip("/")
    match = re.match(r"^(@[^/]+/[^@/]+|[^@/]+)@([^()]+)", clean)
    if match:
        return match.group(1), match.group(2)
    name, version = raw.get("name"), raw.get("version")
    return (name, version) if isinstance(name, str) and isinstance(version, str) else None


def _yarn(text: str, manifest_text: str | None) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")) and line.endswith(":"):
            descriptor = line[:-1].strip().strip('"').split(",", 1)[0]
            name = _descriptor_name(descriptor)
            current = {"name": name, "raw_dependencies": {}} if name else None
        elif current and re.match(r"^  version ", line):
            current["version"] = line.split("version", 1)[1].strip().strip('"')
            if current.get("name") and current.get("version"):
                records[f"{current['name']}@{current['version']}"] = current
        elif current and re.match(r"^  (?:integrity|checksum) ", line):
            current["integrity"] = line.strip().split(maxsplit=1)[1].strip('"')
        elif current and re.match(r"^    [^ ]+ ", line):
            dependency = line.strip().split(maxsplit=1)[0].strip('"')
            current["raw_dependencies"][dependency] = ""
    if not records:
        raise ValueError("yarn.lock does not contain package entries")
    warnings: list[str] = []
    _resolve_dependencies(records, warnings, "Yarn")
    manifest = _optional_json(manifest_text, "package.json", warnings)
    roots = _resolve_roots({**_dict(manifest.get("dependencies")), **_dict(manifest.get("optionalDependencies"))}, records, warnings, "Yarn")
    dev = _resolve_roots(_dict(manifest.get("devDependencies")), records, warnings, "Yarn")
    return _snapshot("npm", "yarn", "yarn.lock", list(records.values()), roots, dev, warnings, manifest.get("name"))


def _bun(text: str) -> dict[str, Any]:
    data = _json_object(_jsonc_to_json(text), "bun.lock")
    raw_packages = data.get("packages")
    if not isinstance(raw_packages, dict):
        raise ValueError("bun.lock must contain a packages object")
    records: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for key, raw in raw_packages.items():
        if not isinstance(raw, list) or not raw or not isinstance(raw[0], str):
            warnings.append(f"Skipped Bun package {key}: package identity is unavailable")
            continue
        identity = _package_identity(raw[0])
        if not identity:
            warnings.append(f"Skipped Bun package {key}: exact registry version is unavailable")
            continue
        package_name, version = identity
        records[f"{package_name}@{version}"] = {
            "name": package_name,
            "version": version,
            "raw_dependencies": _dict(raw[2]) if len(raw) > 2 else {},
            "integrity": raw[3] if len(raw) > 3 and isinstance(raw[3], str) else None,
        }
    _resolve_dependencies(records, warnings, "Bun")
    workspaces = _dict(data.get("workspaces"))
    root = workspaces.get("") if isinstance(workspaces.get(""), dict) else {}
    roots = _resolve_roots({**_dict(root.get("dependencies")), **_dict(root.get("optionalDependencies"))}, records, warnings, "Bun")
    dev = _resolve_roots(_dict(root.get("devDependencies")), records, warnings, "Bun")
    return _snapshot("npm", "bun", "bun.lock", list(records.values()), roots, dev, warnings, root.get("name"))


def _pipfile(text: str) -> dict[str, Any]:
    data = _json_object(text, "Pipfile.lock")
    if not isinstance(data.get("default"), dict) or not isinstance(data.get("develop"), dict):
        raise ValueError('Pipfile.lock must contain "default" and "develop" objects')
    warnings: list[str] = []
    prod = _python_section(data["default"], warnings)
    dev = _python_section(data["develop"], warnings)
    return _snapshot("PyPI", "pipenv", "Pipfile.lock", prod + dev, _roots(prod), _roots(dev), warnings + ["Pipfile.lock does not record transitive dependency edges; dependency paths are unavailable."])


def _python_section(section: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    packages = []
    for name, raw in section.items():
        value = raw if isinstance(raw, str) else raw.get("version") if isinstance(raw, dict) else None
        if not isinstance(value, str) or not value.startswith("==") or not _exact(value[2:]):
            warnings.append(f"Skipped {name}: Pipfile.lock entry does not contain an exact == version")
            continue
        packages.append({"name": name, "version": value[2:]})
    return packages


def _requirements(text: str) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for index, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"\s+#.*$", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^]]+\])?==([^\s;]+)(?:\s*;.*)?$", line)
        if not match or not _exact(match.group(2)):
            warnings.append(f"Skipped requirements.txt line {index}: an exact == version is required")
            continue
        name, version = match.groups()
        key = re.sub(r"[-_.]+", "-", name.lower())
        if key in records and records[key]["version"] != version:
            warnings.append(f"Skipped conflicting requirement {name}=={version}; {records[key]['version']} was already selected")
            continue
        records[key] = {"name": name, "version": version}
    packages = list(records.values())
    return _snapshot("PyPI", "pip", "requirements.txt", packages, _roots(packages), warnings=warnings + ["requirements.txt does not record transitive dependency edges; every pinned entry is treated as direct."])


def _go_sum(text: str, go_mod: str | None) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1].endswith("/go.mod"):
            continue
        name, version, checksum = parts
        records[f"{name}@{version}"] = {"name": name, "version": version, "integrity": checksum}
    warnings = ["go.sum records verified module versions but not a complete resolved dependency graph; dependency paths are unavailable."]
    direct: dict[str, str] = {}
    if go_mod is None:
        warnings.insert(0, "go.mod was not found; direct dependencies are unknown.")
    else:
        direct = _go_mod_requires(go_mod)
    installed = {(value["name"], value["version"]) for value in records.values()}
    roots = {name: version for name, version in direct.items() if (name, version) in installed}
    return _snapshot("Go", "go-modules", "go.sum", list(records.values()), roots, warnings=warnings)


def _go_mod_requires(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line == "require (":
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        body = line if in_block else line[8:].strip() if line.startswith("require ") else ""
        parts = body.split()
        if len(parts) >= 2:
            result[parts[0]] = parts[1]
    return result


def _cargo(text: str) -> dict[str, Any]:
    data = tomllib.loads(text)
    raw_packages = data.get("package")
    if not isinstance(raw_packages, list):
        raise ValueError("Cargo.lock does not contain package entries")
    external = [item for item in raw_packages if isinstance(item, dict) and item.get("source") and item.get("name") and item.get("version")]
    records = {f"{item['name']}@{item['version']}": {"name": item["name"], "version": str(item["version"]), "integrity": item.get("checksum"), "raw_dependencies": {str(dep).split()[0]: "" for dep in item.get("dependencies", [])}} for item in external}
    warnings: list[str] = []
    _resolve_dependencies(records, warnings, "Cargo")
    roots: dict[str, str] = {}
    for root in (item for item in raw_packages if isinstance(item, dict) and not item.get("source")):
        roots.update(_resolve_roots({str(dep).split()[0]: "" for dep in root.get("dependencies", [])}, records, warnings, "Cargo"))
    if not roots:
        warnings.append("Cargo workspace roots are unavailable; direct dependencies are unknown.")
    return _snapshot("crates.io", "cargo", "Cargo.lock", list(records.values()), roots, warnings=warnings)


def _maven(text: str) -> dict[str, Any]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("pom.xml is not valid XML") from exc
    namespace = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
    value = lambda node, key: (node.findtext(f"{namespace}{key}") or "").strip()
    prod: list[dict[str, Any]] = []
    dev: list[dict[str, Any]] = []
    warnings = ["pom.xml contains direct declarations only; resolved transitive dependencies require a lockfile."]
    dependencies = root.find(f"{namespace}dependencies")
    for dependency in dependencies if dependencies is not None else []:
        group, artifact, version = value(dependency, "groupId"), value(dependency, "artifactId"), value(dependency, "version")
        if not group or not artifact or not _exact(version) or re.search(r"[${}\[\]()]", version) or version.upper() in {"LATEST", "RELEASE"}:
            warnings.append(f"Skipped Maven dependency {group + ':' + artifact if group and artifact else '(unknown)'}: an exact version is required")
            continue
        (dev if value(dependency, "scope") == "test" else prod).append({"name": f"{group}:{artifact}", "version": version})
    parent = root.find(f"{namespace}parent")
    group = value(root, "groupId") or value(parent if parent is not None else ElementTree.Element("parent"), "groupId")
    artifact = value(root, "artifactId")
    return _snapshot("Maven", "maven", "pom.xml", prod + dev, _roots(prod), _roots(dev), warnings, f"{group}:{artifact}" if group and artifact else artifact or None)


def _gradle(text: str) -> dict[str, Any]:
    packages: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("empty="):
            continue
        parts = line.split("=", 1)[0].split(":")
        if len(parts) != 3 or not all(parts) or not _exact(parts[2]) or parts[2].endswith("+"):
            warnings.append(f"Skipped Gradle lock line {index}: expected exact group:artifact:version")
            continue
        packages.append({"name": f"{parts[0]}:{parts[1]}", "version": parts[2]})
    return _snapshot("Maven", "gradle", "gradle.lockfile", packages, warnings=warnings + ["Gradle lock state does not identify direct dependencies or dependency edges."])


def _nuget(text: str) -> dict[str, Any]:
    data = _json_object(text, "packages.lock.json")
    frameworks = data.get("dependencies")
    if not isinstance(frameworks, dict):
        raise ValueError("packages.lock.json must contain a dependencies object")
    records: dict[str, dict[str, Any]] = {}
    direct: set[str] = set()
    warnings: list[str] = []
    for framework in frameworks.values():
        if not isinstance(framework, dict):
            continue
        for name, raw in framework.items():
            if not isinstance(raw, dict) or not isinstance(raw.get("resolved"), str):
                warnings.append(f"Skipped NuGet package {name}: exact resolved version is unavailable")
                continue
            version = raw["resolved"]
            key = f"{name.lower()}@{version}"
            records[key] = {"name": name, "version": version, "integrity": raw.get("contentHash"), "raw_dependencies": _dict(raw.get("dependencies"))}
            if raw.get("type") in ("Direct", "Project"):
                direct.add(key)
    _resolve_dependencies(records, warnings, "NuGet")
    roots = {records[key]["name"]: records[key]["version"] for key in direct}
    return _snapshot("NuGet", "nuget", "packages.lock.json", list(records.values()), roots, warnings=warnings)


def _gemfile(text: str) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    direct: list[str] = []
    section = ""
    current: dict[str, Any] | None = None
    in_specs = False
    for line in text.splitlines():
        if re.fullmatch(r"[A-Z][A-Z ]+", line):
            section, in_specs, current = line.strip(), False, None
            continue
        if section == "GEM" and line.strip() == "specs:":
            in_specs = True
            continue
        package_match = re.match(r"^    ([^\s(]+) \(([^)\s]+)(?: [^)]+)?\)$", line) if in_specs else None
        if package_match:
            name, version = package_match.groups()
            current = {"name": name, "version": version, "raw_dependencies": {}}
            records[f"{name}@{version}"] = current
            continue
        dependency_match = re.match(r"^      ([^\s(]+)", line) if current else None
        if dependency_match:
            current["raw_dependencies"][dependency_match.group(1)] = ""
        elif section == "DEPENDENCIES":
            match = re.match(r"^  ([^\s!(]+)", line)
            if match:
                direct.append(match.group(1))
    if not records:
        raise ValueError("Gemfile.lock does not contain GEM specs")
    warnings: list[str] = []
    _resolve_dependencies(records, warnings, "Bundler")
    roots = _resolve_roots({name: "" for name in direct}, records, warnings, "Bundler")
    return _snapshot("RubyGems", "bundler", "Gemfile.lock", list(records.values()), roots, warnings=warnings)


def _pub(text: str) -> dict[str, Any]:
    data = _yaml_object(text, "pubspec.lock")
    raw_packages = data.get("packages")
    if not isinstance(raw_packages, dict):
        raise ValueError("pubspec.lock must contain a packages object")
    packages: list[dict[str, Any]] = []
    roots: dict[str, str] = {}
    dev: dict[str, str] = {}
    warnings = ["pubspec.lock does not record dependency edges; dependency paths are unavailable."]
    for name, raw in raw_packages.items():
        version = raw.get("version") if isinstance(raw, dict) else None
        if not isinstance(version, str) or not _exact(version):
            warnings.append(f"Skipped Pub package {name}: exact version is unavailable")
            continue
        packages.append({"name": name, "version": version})
        if raw.get("dependency") == "direct main":
            roots[name] = version
        elif raw.get("dependency") == "direct dev":
            dev[name] = version
    return _snapshot("Pub", "pub", "pubspec.lock", packages, roots, dev, warnings)


def _swift(text: str) -> dict[str, Any]:
    data = _json_object(text, "Package.resolved")
    object_value = data.get("object") if isinstance(data.get("object"), dict) else {}
    pins = data.get("pins") if isinstance(data.get("pins"), list) else object_value.get("pins")
    if not isinstance(pins, list):
        raise ValueError("Package.resolved must contain a pins array")
    packages: list[dict[str, Any]] = []
    warnings = ["Package.resolved does not identify direct dependencies or dependency edges."]
    for pin in pins:
        state = pin.get("state") if isinstance(pin, dict) and isinstance(pin.get("state"), dict) else {}
        name = pin.get("identity") or pin.get("package") if isinstance(pin, dict) else None
        version = state.get("version")
        if not isinstance(name, str) or not isinstance(version, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
            warnings.append(f"Skipped Swift package {name or '(unknown)'}: a semantic version is unavailable")
            continue
        packages.append({"name": name, "version": version, "integrity": state.get("revision")})
    return _snapshot("SwiftURL", "swift-package-manager", "Package.resolved", packages, warnings=warnings)


def _resolve_dependencies(records: dict[str, dict[str, Any]], warnings: list[str], label: str) -> None:
    by_name: dict[str, list[str]] = defaultdict(list)
    for package in records.values():
        if package["version"] not in by_name[package["name"]]:
            by_name[package["name"]].append(package["version"])
    for package in records.values():
        resolved: dict[str, str] = {}
        for name in package.pop("raw_dependencies", {}):
            versions = by_name.get(name, [])
            if len(versions) == 1:
                resolved[name] = versions[0]
            else:
                warnings.append(f"Could not uniquely resolve {label} dependency {name} declared by {package['name']}@{package['version']}")
        package["dependencies"] = resolved


def _resolve_roots(raw: dict[str, Any], records: dict[str, dict[str, Any]], warnings: list[str], label: str) -> dict[str, str]:
    by_name: dict[str, list[str]] = defaultdict(list)
    for package in records.values():
        if package["version"] not in by_name[package["name"]]:
            by_name[package["name"]].append(package["version"])
    result: dict[str, str] = {}
    for name in raw:
        versions = by_name.get(name, [])
        if len(versions) == 1:
            result[name] = versions[0]
        else:
            warnings.append(f"Could not uniquely resolve direct {label} package {name}")
    return result


def _roots(packages: list[dict[str, Any]]) -> dict[str, str]:
    return {package["name"]: package["version"] for package in packages}


def _descriptor_name(value: str) -> str | None:
    if value.startswith("@"):
        separator = value.find("@", value.find("/") + 1)
    else:
        separator = value.find("@")
    return value[:separator] if separator > 0 else None


def _package_identity(value: str) -> tuple[str, str] | None:
    separator = value.find("@", value.find("/") + 1) if value.startswith("@") else value.rfind("@")
    if separator <= 0:
        return None
    name, version = value[:separator], value[separator + 1 :]
    if not name or not _exact(version) or version.startswith(("workspace:", "file:", "link:", "git")):
        return None
    return name, version


def _exact(value: str) -> bool:
    return bool(value) and not re.search(r"[\s*?]", value) and not re.match(r"^(?:[<>=~^]|latest$)", value, re.I)


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _yaml_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{label} is not valid YAML") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def _optional_json(text: str | None, label: str, warnings: list[str]) -> dict[str, Any]:
    if text is None:
        warnings.append(f"{label} was not found; direct dependencies are unknown.")
        return {}
    try:
        return _json_object(text, label)
    except ValueError:
        warnings.append(f"{label} is invalid; direct dependencies are unknown.")
        return {}


def _jsonc_to_json(text: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    without_comments = re.sub(r"(^|\s)//.*$", lambda match: match.group(1), without_blocks, flags=re.M)
    return re.sub(r",\s*([}\]])", r"\1", without_comments)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _optional_text(value: str | bytes | None) -> str | None:
    return _text(value) if value is not None else None
