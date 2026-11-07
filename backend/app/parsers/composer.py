"""Server-side composer.lock normalization for GitHub repository scans.

Mirrors the VS Code extension's `parseComposerLockfile` (extension/src/utils/workspace.ts) so
GitHub scans submit the same normalized lockfile shape the backend already understands.
Contents are only parsed as JSON and never executed.
"""

from __future__ import annotations

import json
import re
from typing import Any


def normalize_composer_lockfile(lock_text: str | bytes, manifest_text: str | bytes | None) -> dict[str, Any]:
    """Convert raw composer.lock (+ optional composer.json) into the normalized lockfile format.

    Raises ValueError with a user-facing message when composer.lock is unusable.
    """
    try:
        data = json.loads(lock_text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("composer.lock is not valid JSON.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list) or not isinstance(data.get("packages-dev"), list):
        raise ValueError('composer.lock must contain "packages" and "packages-dev" arrays.')

    warnings: list[str] = []
    manifest: dict[str, Any] = {}
    if manifest_text is None:
        warnings.append("composer.json was not found; direct dependencies are unknown")
    else:
        try:
            parsed = json.loads(manifest_text)
            if isinstance(parsed, dict):
                manifest = parsed
            else:
                warnings.append("composer.json does not contain a JSON object; direct dependencies are unknown")
        except (json.JSONDecodeError, UnicodeDecodeError):
            warnings.append("composer.json is invalid; direct dependencies are unknown")

    raw_packages = [*data["packages"], *data["packages-dev"]]
    versions: dict[str, str] = {
        raw["name"]: _normalize_version(raw["version"])
        for raw in raw_packages
        if isinstance(raw, dict) and isinstance(raw.get("name"), str) and isinstance(raw.get("version"), str)
    }

    manifest_name = manifest.get("name") if isinstance(manifest.get("name"), str) else None
    packages: dict[str, Any] = {
        "": _drop_none(
            {
                "name": manifest_name,
                "dependencies": _dependency_map(manifest.get("require"), versions),
                "devDependencies": _dependency_map(manifest.get("require-dev"), versions),
            }
        )
    }
    for raw in raw_packages:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not isinstance(raw.get("version"), str):
            warnings.append("Skipped a Composer package whose name or exact version is unavailable")
            continue
        dist = raw.get("dist") if isinstance(raw.get("dist"), dict) else {}
        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        resolved = dist.get("url") if isinstance(dist.get("url"), str) else source.get("url") if isinstance(source.get("url"), str) else None
        shasum = dist.get("shasum")
        packages[f"vendor/{raw['name']}"] = _drop_none(
            {
                "name": raw["name"],
                "version": _normalize_version(raw["version"]),
                "resolved": resolved,
                "integrity": shasum if isinstance(shasum, str) and shasum else None,
                "dependencies": _dependency_map(raw.get("require"), versions),
            }
        )

    return _drop_none(
        {
            "name": manifest_name,
            "lockfileVersion": 3,
            "packages": packages,
            "provenyxSource": "composer.lock",
            "provenyxEcosystem": "Packagist",
            "provenyxPackageManager": "composer",
            "provenyxWarnings": warnings,
        }
    )


def _dependency_map(value: Any, versions: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {name: versions[name] for name in value if name in versions}


def _normalize_version(version: str) -> str:
    return version[1:] if re.match(r"^v\d", version) else version


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: entry for key, entry in value.items() if entry is not None}
