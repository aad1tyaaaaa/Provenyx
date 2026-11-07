from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes import get_scan_service
from app.main import app
from app.models.database import ScanRepository
from app.parsers.ecosystems import SUPPORTED_DEPENDENCY_FILES, normalize_dependency_file
from app.parsers.lockfile import parse_lockfile
from app.parsers.npm_registry import NpmRegistryClient
from app.services import ScanService


CASES = [
    ("package-lock.json", '{"name":"npm-app","lockfileVersion":3,"packages":{"":{"dependencies":{"left-pad":"1.3.0"}},"node_modules/left-pad":{"version":"1.3.0"}}}', {}, "npm", "npm", "left-pad"),
    ("pnpm-lock.yaml", "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies:\n      left-pad:\n        version: 1.3.0\npackages:\n  left-pad@1.3.0:\n    resolution: {integrity: sha512-test}\n", {}, "npm", "pnpm", "left-pad"),
    ("yarn.lock", '# yarn lockfile v1\n\nleft-pad@^1.0.0:\n  version "1.3.0"\n  integrity sha512-test\n', {"package.json": '{"name":"yarn-app","dependencies":{"left-pad":"^1.0.0"}}'}, "npm", "yarn", "left-pad"),
    ("bun.lock", '{\n// comment\n"lockfileVersion":1,"workspaces":{"":{"dependencies":{"left-pad":"^1"}}},"packages":{"left-pad":["left-pad@1.3.0","",{},"sha512-test"],},\n}', {}, "npm", "bun", "left-pad"),
    ("composer.lock", '{"packages":[{"name":"symfony/console","version":"v7.1.3","require":{}}],"packages-dev":[]}', {"composer.json": '{"name":"demo/php","require":{"symfony/console":"^7"}}'}, "Packagist", "composer", "symfony/console"),
    ("Pipfile.lock", '{"default":{"requests":{"version":"==2.32.3"}},"develop":{}}', {}, "PyPI", "pipenv", "requests"),
    ("requirements.txt", "requests==2.32.3\nflask>=3\n", {}, "PyPI", "pip", "requests"),
    ("go.sum", "github.com/google/uuid v1.6.0 h1:test\ngithub.com/google/uuid v1.6.0/go.mod h1:mod\n", {"go.mod": "module example.test/app\nrequire github.com/google/uuid v1.6.0\n"}, "Go", "go-modules", "github.com/google/uuid"),
    ("Cargo.lock", 'version = 3\n[[package]]\nname = "app"\nversion = "0.1.0"\ndependencies = ["serde 1.0.210"]\n[[package]]\nname = "serde"\nversion = "1.0.210"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "test"\n', {}, "crates.io", "cargo", "serde"),
    ("pom.xml", "<project><modelVersion>4.0.0</modelVersion><groupId>com.test</groupId><artifactId>app</artifactId><dependencies><dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId><version>2.0.16</version></dependency></dependencies></project>", {}, "Maven", "maven", "org.slf4j:slf4j-api"),
    ("gradle.lockfile", "com.google.guava:guava:33.3.0-jre=runtimeClasspath\n", {}, "Maven", "gradle", "com.google.guava:guava"),
    ("packages.lock.json", '{"version":1,"dependencies":{"net8.0":{"Newtonsoft.Json":{"type":"Direct","resolved":"13.0.3","contentHash":"test"}}}}', {}, "NuGet", "nuget", "Newtonsoft.Json"),
    ("Gemfile.lock", "GEM\n  remote: https://rubygems.org/\n  specs:\n    rack (3.1.7)\n\nDEPENDENCIES\n  rack (~> 3.1)\n", {}, "RubyGems", "bundler", "rack"),
    ("pubspec.lock", 'packages:\n  http:\n    dependency: "direct main"\n    source: hosted\n    version: "1.2.2"\n', {}, "Pub", "pub", "http"),
    ("Package.resolved", '{"version":2,"pins":[{"identity":"swift-nio","state":{"revision":"abc","version":"2.72.0"}}]}', {}, "SwiftURL", "swift-package-manager", "swift-nio"),
]


@pytest.mark.parametrize(("filename", "content", "auxiliaries", "ecosystem", "manager", "package"), CASES)
def test_normalizes_every_supported_dependency_file(
    filename: str,
    content: str,
    auxiliaries: dict[str, str],
    ecosystem: str,
    manager: str,
    package: str,
) -> None:
    snapshot = normalize_dependency_file(filename, content, auxiliaries)
    parsed = parse_lockfile(snapshot)

    assert parsed.ecosystem == ecosystem
    assert parsed.package_manager == manager
    assert package in {entry.name for entry in parsed.packages}


def test_fixture_matrix_matches_supported_filename_contract() -> None:
    assert {case[0] for case in CASES} == set(SUPPORTED_DEPENDENCY_FILES)


def test_dynamic_versions_are_skipped_with_warnings() -> None:
    snapshot = normalize_dependency_file("requirements.txt", "safe==1.2.3\ndynamic==1.*\n")
    parsed = parse_lockfile(snapshot)

    assert {entry.name for entry in parsed.packages} == {"safe"}
    assert any("line 2" in warning for warning in parsed.warnings)


def test_manual_dependency_upload_uses_shared_normalizer(tmp_path) -> None:
    service = ScanService(
        ScanRepository(f"sqlite:///{tmp_path / 'manual.db'}"),
        NpmRegistryClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)), retries=0),
    )
    app.dependency_overrides[get_scan_service] = lambda: service
    try:
        with TestClient(app) as client:
            accepted = client.post(
                "/api/dependency-scans",
                json={
                    "filename": "requirements.txt",
                    "content": "requests==2.32.3\nflask>=3\n",
                    "project_name": "python-app",
                    "client": "dashboard/0.1.0",
                },
            )
            assert accepted.status_code == 202
            detail = client.get(f"/api/scans/{accepted.json()['scan_id']}")
    finally:
        app.dependency_overrides.clear()

    assert detail.status_code == 200
    assert detail.json()["status"] == "complete"
    assert detail.json()["ecosystem"] == "PyPI"
    assert detail.json()["package_manager"] == "pip"
    assert any("line 2" in warning for warning in detail.json()["warnings"])


def test_manual_dependency_upload_rejects_bad_filename() -> None:
    with TestClient(app) as client:
        response = client.post("/api/dependency-scans", json={"filename": "source.py", "content": "print('no')"})
    assert response.status_code == 422
