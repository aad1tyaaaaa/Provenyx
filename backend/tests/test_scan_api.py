from collections.abc import Iterator
from pathlib import Path
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes import get_scan_service
from app.detection.osv import OsvClient
from tests.fakes import osv_transport
from app.main import app
from app.models.database import ScanRepository
from app.parsers.npm_registry import NpmRegistryClient
from app.services import ScanService


def test_existing_scan_database_adds_owner_column(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE scans (
                scan_id VARCHAR(36) PRIMARY KEY,
                status VARCHAR(16) NOT NULL,
                created_at DATETIME NOT NULL,
                project_name VARCHAR(214),
                lockfile JSON NOT NULL,
                summary JSON,
                graph JSON,
                warnings JSON NOT NULL,
                error TEXT
            )"""
        )

    ScanRepository(f"sqlite:///{database_path}")

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(scans)")}
    assert "owner_id" in columns


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, ScanService]]:
    def registry_handler(request: httpx.Request) -> httpx.Response:
        package = request.url.path.split("/")[-2]
        version = request.url.path.split("/")[-1]
        return httpx.Response(
            200,
            json={
                "name": package,
                "version": version,
                "description": f"Metadata for {package}",
                "license": "MIT",
                "dist": {"integrity": f"sha512-registry-{version}"},
            },
        )

    repository = ScanRepository(f"sqlite:///{tmp_path / 'scans.db'}")
    registry = NpmRegistryClient(transport=httpx.MockTransport(registry_handler), retries=0)
    service = ScanService(repository, registry)
    app.dependency_overrides[get_scan_service] = lambda: service
    with TestClient(app) as client:
        yield client, service
    app.dependency_overrides.clear()


def test_scan_completes_and_exposes_persisted_summary_and_graph(api: tuple[TestClient, ScanService]):
    client, service = api
    lockfile = {
        "name": "api-demo",
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "api-demo", "dependencies": {"parent": "1.0.0"}},
            "node_modules/parent": {
                "version": "1.0.0",
                "resolved": "https://registry.npmjs.org/parent/-/parent-1.0.0.tgz",
                "integrity": "sha512-lockfile-parent",
                "dependencies": {"child": "^2.0.0"},
            },
            "node_modules/child": {"version": "2.1.0"},
        },
    }

    accepted = client.post("/api/scans", json={"lockfile": lockfile})
    assert accepted.status_code == 202
    scan_id = accepted.json()["scan_id"]

    detail = client.get(f"/api/scans/{scan_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "complete"
    assert detail.json()["project_name"] == "api-demo"
    assert detail.json()["summary"] == {"packages": 2, "direct": 1, "transitive": 1, "findings": 0}

    graph = client.get(f"/api/scans/{scan_id}/graph")
    assert graph.status_code == 200
    body = graph.json()
    assert body["status"] == "complete"
    assert [(edge["from"], edge["to"], edge["type"]) for edge in body["edges"]] == [
        ("parent@1.0.0", "child@2.1.0", "prod")
    ]
    parent = next(node for node in body["nodes"] if node["id"] == "parent@1.0.0")
    assert parent["direct"] is True
    assert parent["resolved_urls"] == ["https://registry.npmjs.org/parent/-/parent-1.0.0.tgz"]
    assert parent["lockfile_integrities"] == ["sha512-lockfile-parent"]
    assert parent["registry"]["status"] == "available"

    reopened = ScanRepository(service.repository._engine.url.render_as_string(hide_password=False))
    assert reopened.get(scan_id) is not None
    assert reopened.get(scan_id).status == "complete"  # type: ignore[union-attr]


def test_scan_preserves_multiple_versions_in_api_graph(api: tuple[TestClient, ScanService]):
    client, _ = api
    lockfile = {
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"a": "1.0.0", "shared": "2.0.0"}},
            "node_modules/a": {"version": "1.0.0", "dependencies": {"shared": "1.0.0"}},
            "node_modules/a/node_modules/shared": {"version": "1.0.0"},
            "node_modules/shared": {"version": "2.0.0"},
        },
    }

    scan_id = client.post("/api/scans", json={"lockfile": lockfile}).json()["scan_id"]
    nodes = client.get(f"/api/scans/{scan_id}/graph").json()["nodes"]

    assert {node["id"] for node in nodes} == {"a@1.0.0", "shared@1.0.0", "shared@2.0.0"}


def test_composer_scan_uses_packagist_osv_without_contacting_npm(tmp_path: Path):
    ecosystems: list[str] = []

    def osv_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/querybatch":
            queries = json.loads(request.content)["queries"]
            ecosystems.extend(query["package"]["ecosystem"] for query in queries)
            return httpx.Response(200, json={"results": [{} for _ in queries]})
        return httpx.Response(404)

    class UnexpectedNpmRegistry(NpmRegistryClient):
        async def fetch_many(self, releases):
            raise AssertionError("Composer scans must not query npm")

    service = ScanService(
        ScanRepository(f"sqlite:///{tmp_path / 'composer.db'}"),
        UnexpectedNpmRegistry(retries=0),
        osv=OsvClient(transport=httpx.MockTransport(osv_handler), retries=0),
    )
    app.dependency_overrides[get_scan_service] = lambda: service
    lockfile = {
        "name": "provenyx/php-demo",
        "lockfileVersion": 3,
        "provenyxSource": "composer.lock",
        "packages": {
            "": {"dependencies": {"symfony/console": "7.1.3"}},
            "vendor/symfony/console": {
                "name": "symfony/console",
                "version": "7.1.3",
                "dependencies": {"symfony/string": "7.1.2"},
            },
            "vendor/symfony/string": {"name": "symfony/string", "version": "7.1.2"},
        },
    }

    with TestClient(app) as client:
        scan_id = client.post("/api/scans", json={"lockfile": lockfile}).json()["scan_id"]
        detail = client.get(f"/api/scans/{scan_id}").json()
        graph = client.get(f"/api/scans/{scan_id}/graph").json()
    app.dependency_overrides.clear()

    assert detail["status"] == "complete"
    assert ecosystems == ["Packagist", "Packagist"]
    assert [(edge["from"], edge["to"]) for edge in graph["edges"]] == [
        ("symfony/console@7.1.3", "symfony/string@7.1.2")
    ]
    assert any("Packagist registry enrichment" in warning for warning in detail["warnings"])


def test_same_project_in_different_ecosystems_does_not_share_a_baseline(api: tuple[TestClient, ScanService]):
    client, _ = api
    npm_lock = {
        "name": "polyglot-app",
        "lockfileVersion": 3,
        "packages": {"": {"dependencies": {"shared": "1.0.0"}}, "node_modules/shared": {"version": "1.0.0"}},
    }
    python_lock = {
        "name": "polyglot-app",
        "lockfileVersion": 3,
        "provenyxEcosystem": "PyPI",
        "provenyxPackageManager": "uv",
        "provenyxComponentPath": "services/api",
        "packages": {"": {"dependencies": {"shared": "2.0.0"}}, "packages/shared": {"name": "shared", "version": "2.0.0"}},
    }

    client.post("/api/scans", json={"lockfile": npm_lock})
    python_id = client.post("/api/scans", json={"lockfile": python_lock}).json()["scan_id"]
    detail = client.get(f"/api/scans/{python_id}").json()
    graph = client.get(f"/api/scans/{python_id}/graph").json()

    assert detail["ecosystem"] == "PyPI"
    assert detail["package_manager"] == "uv"
    assert detail["component_path"] == "services/api"
    assert detail["summary"]["findings"] == 0
    assert any("No earlier completed scan" in warning for warning in detail["warnings"])
    assert graph["nodes"][0]["purl"] == "pkg:pypi/shared@2.0.0"


def test_registry_failure_is_unknown_metadata_not_scan_failure(tmp_path: Path):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("offline", request=request)

    service = ScanService(
        ScanRepository(f"sqlite:///{tmp_path / 'offline.db'}"),
        NpmRegistryClient(transport=httpx.MockTransport(timeout), retries=0),
    )
    app.dependency_overrides[get_scan_service] = lambda: service
    with TestClient(app) as client:
        scan_id = client.post(
            "/api/scans",
            json={
                "lockfile": {
                    "lockfileVersion": 3,
                    "packages": {"": {"dependencies": {"pkg": "1.0.0"}}, "node_modules/pkg": {"version": "1.0.0"}},
                }
            },
        ).json()["scan_id"]
        graph = client.get(f"/api/scans/{scan_id}/graph").json()
    app.dependency_overrides.clear()

    assert graph["status"] == "complete"
    assert graph["nodes"][0]["registry"]["status"] == "unavailable"
    assert "ReadTimeout" in graph["nodes"][0]["registry"]["error"]


def test_first_scan_reports_exact_version_osv_advisory(tmp_path: Path):
    npm = NpmRegistryClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"name": "minimist", "version": "1.2.5"})
        ),
        retries=0,
    )
    advisory = {
        "id": "GHSA-test-1234",
        "aliases": ["CVE-2026-0001"],
        "summary": "Synthetic prototype-pollution advisory",
        "database_specific": {"severity": "HIGH"},
    }
    osv = OsvClient(transport=osv_transport({("minimist", "1.2.5"): [advisory]}), retries=0)
    service = ScanService(
        ScanRepository(f"sqlite:///{tmp_path / 'osv.db'}"),
        npm,
        osv=osv,
    )
    app.dependency_overrides[get_scan_service] = lambda: service
    with TestClient(app) as client:
        scan_id = client.post(
            "/api/scans",
            json={
                "lockfile": {
                    "name": "vulnerable-first-scan",
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"dependencies": {"minimist": "1.2.5"}},
                        "node_modules/minimist": {"version": "1.2.5"},
                    },
                }
            },
        ).json()["scan_id"]
        findings = client.get(f"/api/scans/{scan_id}/findings").json()
        detail = client.get(f"/api/scans/{scan_id}").json()
    app.dependency_overrides.clear()

    assert detail["summary"]["findings"] == 1
    assert findings[0]["rule_id"] == "known-vulnerability"
    assert findings[0]["package"] == "minimist"
    assert findings[0]["evidence"][0]["source"] == "osv"


def test_analysis_failure_is_recorded_without_echoing_lockfile(tmp_path: Path):
    class BrokenRegistry:
        async def fetch_many(self, releases):
            raise RuntimeError("secret lockfile content")

    service = ScanService(ScanRepository(f"sqlite:///{tmp_path / 'failed.db'}"), BrokenRegistry())  # type: ignore[arg-type]
    app.dependency_overrides[get_scan_service] = lambda: service
    with TestClient(app) as client:
        scan_id = client.post(
            "/api/scans",
            json={
                "lockfile": {
                    "lockfileVersion": 3,
                    "packages": {"": {"dependencies": {"pkg": "1.0.0"}}, "node_modules/pkg": {"version": "1.0.0"}},
                }
            },
        ).json()["scan_id"]
        detail = client.get(f"/api/scans/{scan_id}").json()
    app.dependency_overrides.clear()

    assert detail["status"] == "failed"
    assert detail["error"] == "Dependency analysis failed: RuntimeError"
    assert "secret" not in detail["error"]


def test_unknown_scan_returns_404(api: tuple[TestClient, ScanService]):
    client, _ = api
    missing_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/scans/{missing_id}").status_code == 404
    assert client.get(f"/api/scans/{missing_id}/graph").status_code == 404


def test_recent_scans_are_persisted_and_newest_first(api: tuple[TestClient, ScanService]):
    client, _ = api
    lockfile = {"name": "history", "lockfileVersion": 3, "packages": {"": {"name": "history"}}}
    first_id = client.post("/api/scans", json={"lockfile": lockfile}).json()["scan_id"]
    second_id = client.post("/api/scans", json={"lockfile": lockfile}).json()["scan_id"]

    scans = client.get("/api/scans")

    assert scans.status_code == 200
    assert [scan["scan_id"] for scan in scans.json()] == [second_id, first_id]


def test_compare_endpoint_uses_persisted_scan_snapshots(api: tuple[TestClient, ScanService]):
    client, _ = api
    baseline = {
        "lockfileVersion": 3,
        "packages": {"": {"dependencies": {"pkg": "1.0.0"}}, "node_modules/pkg": {"version": "1.0.0"}},
    }
    current = {
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"pkg": "2.0.0", "added": "1.0.0"}},
            "node_modules/pkg": {"version": "2.0.0"},
            "node_modules/added": {"version": "1.0.0"},
        },
    }
    baseline_id = client.post("/api/scans", json={"lockfile": baseline}).json()["scan_id"]
    current_id = client.post("/api/scans", json={"lockfile": current}).json()["scan_id"]

    response = client.get(f"/api/scans/{current_id}/compare/{baseline_id}")

    assert response.status_code == 200
    assert response.json()["added_packages"] == ["added@1.0.0", "pkg@2.0.0"]
    assert response.json()["removed_packages"] == ["pkg@1.0.0"]
    assert response.json()["version_changes"] == [
        {"name": "pkg", "from_versions": ["1.0.0"], "to_versions": ["2.0.0"]}
    ]


def test_project_baseline_generates_persisted_evidence_backed_findings(api: tuple[TestClient, ScanService]):
    client, service = api
    baseline = {
        "lockfileVersion": 3,
        "packages": {"": {"dependencies": {"parent": "1.0.0"}}, "node_modules/parent": {"version": "1.0.0"}},
    }
    suspicious = {
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"parent": "1.0.0"}},
            "node_modules/parent": {"version": "1.0.0", "dependencies": {"new-child": "1.0.0"}},
            "node_modules/new-child": {"version": "1.0.0"},
        },
    }
    baseline_id = client.post(
        "/api/scans", json={"lockfile": baseline, "project_name": "finding-demo"}
    ).json()["scan_id"]
    current_id = client.post(
        "/api/scans", json={"lockfile": suspicious, "project_name": "finding-demo"}
    ).json()["scan_id"]

    response = client.get(f"/api/scans/{current_id}/findings")
    detail = client.get(f"/api/scans/{current_id}").json()

    assert response.status_code == 200
    assert detail["summary"]["findings"] == 2
    assert {finding["rule_id"] for finding in response.json()} == {"new-dependency", "dependency-expansion"}
    assert all(finding["evidence"] for finding in response.json())
    assert next(f for f in response.json() if f["rule_id"] == "new-dependency")["paths"] == [
        ["root", "parent@1.0.0", "new-child@1.0.0"]
    ]

    reopened = ScanRepository(service.repository._engine.url.render_as_string(hide_password=False))
    assert len(reopened.get_findings(current_id)) == 2
    assert reopened.get_findings(baseline_id) == []


def test_normal_upgrade_does_not_trigger_unjustified_finding(api: tuple[TestClient, ScanService]):
    client, _ = api

    def lockfile(version: str):
        return {
            "lockfileVersion": 3,
            "packages": {"": {"dependencies": {"pkg": version}}, "node_modules/pkg": {"version": version}},
        }

    client.post("/api/scans", json={"lockfile": lockfile("1.0.0"), "project_name": "benign-upgrade"})
    scan_id = client.post(
        "/api/scans", json={"lockfile": lockfile("2.0.0"), "project_name": "benign-upgrade"}
    ).json()["scan_id"]

    assert client.get(f"/api/scans/{scan_id}/findings").json() == []
    assert client.get(f"/api/scans/{scan_id}").json()["summary"]["findings"] == 0


def test_unusual_release_timing_flows_through_findings_api(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if len(parts) == 1:
            return httpx.Response(
                200,
                json={
                    "time": {
                        "1.0.0": "2026-10-12T10:00:00Z",
                        "2.0.0": "2026-10-12T10:10:00Z",
                    }
                },
            )
        return httpx.Response(200, json={"name": "pkg", "version": parts[-1]})

    service = ScanService(
        ScanRepository(f"sqlite:///{tmp_path / 'timing.db'}"),
        NpmRegistryClient(transport=httpx.MockTransport(handler), retries=0),
    )
    app.dependency_overrides[get_scan_service] = lambda: service

    def lockfile(version: str):
        return {
            "lockfileVersion": 3,
            "packages": {"": {"dependencies": {"pkg": version}}, "node_modules/pkg": {"version": version}},
        }

    with TestClient(app) as client:
        client.post("/api/scans", json={"lockfile": lockfile("1.0.0"), "project_name": "timing-demo"})
        scan_id = client.post(
            "/api/scans", json={"lockfile": lockfile("2.0.0"), "project_name": "timing-demo"}
        ).json()["scan_id"]
        findings = client.get(f"/api/scans/{scan_id}/findings").json()
    app.dependency_overrides.clear()

    assert len(findings) == 1
    assert findings[0]["rule_id"] == "unusual-release-timing"
    assert findings[0]["evidence"][0]["source"] == "npm-registry"


def test_publisher_change_flows_through_findings_api(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if len(parts) == 1:
            return httpx.Response(200, json={"time": {}})
        version = parts[-1]
        publisher = "original-owner" if version == "1.0.0" else "new-owner"
        return httpx.Response(
            200,
            json={"name": "pkg", "version": version, "_npmUser": {"name": publisher}},
        )

    service = ScanService(
        ScanRepository(f"sqlite:///{tmp_path / 'publisher.db'}"),
        NpmRegistryClient(transport=httpx.MockTransport(handler), retries=0),
    )
    app.dependency_overrides[get_scan_service] = lambda: service

    def lockfile(version: str):
        return {
            "lockfileVersion": 3,
            "packages": {"": {"dependencies": {"pkg": version}}, "node_modules/pkg": {"version": version}},
        }

    with TestClient(app) as client:
        client.post("/api/scans", json={"lockfile": lockfile("1.0.0"), "project_name": "publisher-demo"})
        scan_id = client.post(
            "/api/scans", json={"lockfile": lockfile("2.0.0"), "project_name": "publisher-demo"}
        ).json()["scan_id"]
        findings = client.get(f"/api/scans/{scan_id}/findings").json()
    app.dependency_overrides.clear()

    assert len(findings) == 1
    assert findings[0]["rule_id"] == "publisher-change"
    assert findings[0]["evidence"][0]["value"]["previous_publishers"] == ["original-owner"]
    assert findings[0]["evidence"][0]["value"]["current_publisher"] == "new-owner"