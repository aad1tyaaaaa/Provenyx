import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import GITHUB_SESSION_COOKIE, get_github_service, get_scan_service
from app.core.config import Settings, settings
from app.github import GitHubService
from app.main import app
from app.models.database import ScanRepository
from app.parsers.ecosystems import SUPPORTED_DEPENDENCY_FILES
from app.parsers.npm_registry import NpmRegistryClient
from app.services import ScanService


def github_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/login/oauth/access_token":
        return httpx.Response(200, json={"access_token": "github-token"})
    if request.url.path == "/user":
        return httpx.Response(
            200,
            json={"id": 1, "login": "octocat", "avatar_url": "https://avatars.example/octocat"},
        )
    if request.url.path == "/user/repos":
        assert request.headers["authorization"] == "Bearer github-token"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "name": "private-app",
                    "full_name": "octocat/private-app",
                    "private": True,
                    "description": "Private npm application",
                    "default_branch": "main",
                    "html_url": "https://github.com/octocat/private-app",
                    "updated_at": "2026-10-12T10:00:00Z",
                    "owner": {"avatar_url": "https://avatars.example/octocat"},
                }
            ],
        )
    if request.url.path == "/repos/octocat/private-app":
        return httpx.Response(200, json={"default_branch": "main"})
    if request.url.path == "/repos/octocat/private-app/git/trees/main":
        assert request.url.params["recursive"] == "1"
        return httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "package-lock.json", "type": "blob"},
                    {"path": "apps/web/package-lock.json", "type": "blob"},
                    {"path": "composer.lock", "type": "blob"},
                    {"path": "node_modules/pkg/package-lock.json", "type": "blob"},
                    *[
                        {"path": f"services/polyglot/{name}", "type": "blob"}
                        for name in SUPPORTED_DEPENDENCY_FILES
                        if name != "package-lock.json"
                    ],
                    {"path": "vendor/pkg/composer.lock", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ]
            },
        )
    if request.url.path == "/repos/octocat/private-app/contents/package-lock.json":
        lockfile = {
            "name": "private-app",
            "lockfileVersion": 3,
            "packages": {"": {"name": "private-app"}},
        }
        encoded = base64.b64encode(json.dumps(lockfile).encode()).decode()
        return httpx.Response(200, json={"type": "file", "size": len(encoded), "content": encoded})
    if request.url.path == "/repos/octocat/private-app/contents/services/polyglot/requirements.txt":
        content = b"requests==2.32.3\nflask>=3\n"
        return httpx.Response(
            200,
            json={"type": "file", "size": len(content), "content": base64.b64encode(content).decode()},
        )
    if request.url.path == "/repos/octocat/private-app/contents/composer.lock":
        lockfile = {
            "packages": [
                {
                    "name": "symfony/console",
                    "version": "v7.1.3",
                    "dist": {"url": "https://api.github.com/repos/symfony/console/zipball/abc", "shasum": ""},
                    "require": {"php": ">=8.2", "psr/log": "^3.0"},
                },
                {"name": "psr/log", "version": "3.0.2"},
            ],
            "packages-dev": [{"name": "phpunit/phpunit", "version": "11.3.0"}],
        }
        encoded = base64.b64encode(json.dumps(lockfile).encode()).decode()
        return httpx.Response(200, json={"type": "file", "size": len(encoded), "content": encoded})
    if request.url.path == "/repos/octocat/private-app/contents/composer.json":
        manifest = {
            "name": "octocat/private-app",
            "require": {"php": "^8.2", "symfony/console": "^7.1"},
            "require-dev": {"phpunit/phpunit": "^11.3"},
        }
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        return httpx.Response(200, json={"type": "file", "size": len(encoded), "content": encoded})
    return httpx.Response(404, json={"message": "Not Found"})


def configured_github() -> GitHubService:
    return GitHubService(
        Settings(
            github_client_id="client-id",
            github_client_secret=SecretStr("client-secret"),
            github_session_secret=SecretStr("session-secret"),
            github_api_url="https://api.github.test",
            github_oauth_url="https://github.test/login/oauth",
        ),
        transport=httpx.MockTransport(github_handler),
    )


def test_github_oauth_lists_repositories_and_scans_lockfile() -> None:
    github = configured_github()
    app.dependency_overrides[get_github_service] = lambda: github

    with TestClient(app, follow_redirects=False) as client:
        login = client.get("/api/github/auth/login")
        assert login.status_code == 307
        query = parse_qs(urlparse(login.headers["location"]).query)
        assert query["client_id"] == ["client-id"]
        assert query["scope"] == ["repo read:user"]

        callback = client.get(
            "/api/github/auth/callback",
            params={"code": "oauth-code", "state": query["state"][0]},
        )
        assert callback.status_code == 307
        assert callback.headers["location"] == "http://localhost:3000/github?connected=1"

        auth = client.get("/api/github/auth/status")
        assert auth.json() == {
            "configured": True,
            "connected": True,
            "login": "octocat",
            "avatar_url": "https://avatars.example/octocat",
        }

        repositories = client.get("/api/github/repositories")
        assert repositories.status_code == 200
        assert repositories.json()[0]["full_name"] == "octocat/private-app"
        assert repositories.json()[0]["private"] is True

        lockfiles = client.get("/api/github/repositories/octocat/private-app/lockfiles")
        expected = ["apps/web/package-lock.json", "composer.lock", "package-lock.json"] + [
            f"services/polyglot/{name}" for name in SUPPORTED_DEPENDENCY_FILES if name != "package-lock.json"
        ]
        assert lockfiles.json() == sorted(expected)

        accepted = client.post(
            "/api/github/scans",
            json={"owner": "octocat", "repository": "private-app"},
        )
        assert accepted.status_code == 202
        scan_id = accepted.json()["scan_id"]
        scan = client.get(f"/api/scans/{scan_id}").json()
        assert scan["status"] == "complete"
        assert scan["project_name"] == "octocat/private-app"

        python_scan = client.post(
            "/api/github/scans",
            json={
                "owner": "octocat",
                "repository": "private-app",
                "lockfile_path": "services/polyglot/requirements.txt",
            },
        )
        assert python_scan.status_code == 202
        python_detail = client.get(f"/api/scans/{python_scan.json()['scan_id']}").json()
        assert python_detail["status"] == "complete"
        assert python_detail["ecosystem"] == "PyPI"
        assert python_detail["package_manager"] == "pip"
        assert python_detail["component_path"] == "services/polyglot"

    app.dependency_overrides.pop(get_github_service, None)


def test_github_composer_scan_normalizes_lockfile_and_classifies_direct_dependencies() -> None:
    github = configured_github()
    app.dependency_overrides[get_github_service] = lambda: github

    with TestClient(app) as client:
        client.cookies.set(
            GITHUB_SESSION_COOKIE, github.codec.encode({"token": "github-token", "subject": "subject-1"}, 3600)
        )
        accepted = client.post(
            "/api/github/scans",
            json={"owner": "octocat", "repository": "private-app", "lockfile_path": "composer.lock"},
        )
        assert accepted.status_code == 202
        scan_id = accepted.json()["scan_id"]
        scan = client.get(f"/api/scans/{scan_id}").json()
        assert scan["status"] == "complete"
        assert scan["project_name"] == "octocat/private-app"

        graph = client.get(f"/api/scans/{scan_id}/graph").json()
        assert graph["ecosystem"] == "Packagist"
        assert graph["package_manager"] == "composer"
        nodes = {node["id"]: node for node in graph["nodes"]}
        assert nodes["symfony/console@7.1.3"]["direct"] is True  # leading "v" is normalized away
        assert nodes["symfony/console@7.1.3"]["purl"] == "pkg:composer/symfony/console@7.1.3"
        assert nodes["phpunit/phpunit@11.3.0"]["direct"] is True
        assert nodes["psr/log@3.0.2"]["direct"] is False

    app.dependency_overrides.pop(get_github_service, None)


def test_github_repositories_require_connection() -> None:
    github = configured_github()
    app.dependency_overrides[get_github_service] = lambda: github
    with TestClient(app) as client:
        response = client.get("/api/github/repositories")
    app.dependency_overrides.pop(get_github_service, None)

    assert response.status_code == 401
    assert response.json()["detail"] == "Connect a GitHub account first"


def test_github_sessions_cannot_read_each_others_scans(tmp_path) -> None:
    def identity_handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(200, json={"id": 1 if token == "token-a" else 2, "login": token})

    github = GitHubService(configured_github().settings, transport=httpx.MockTransport(identity_handler))
    service = ScanService(
        ScanRepository(f"sqlite:///{tmp_path / 'isolated.db'}"),
        NpmRegistryClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)), retries=0),
    )
    app.dependency_overrides[get_github_service] = lambda: github
    app.dependency_overrides[get_scan_service] = lambda: service

    with TestClient(app) as client:
        client.cookies.set(GITHUB_SESSION_COOKIE, github.codec.encode({"token": "token-a"}, 3600))
        own_scan = client.post(
            "/api/scans",
            json={"lockfile": {"name": "alice", "lockfileVersion": 3, "packages": {"": {"name": "alice"}}}},
        ).json()["scan_id"]

        client.cookies.set(GITHUB_SESSION_COOKIE, github.codec.encode({"token": "token-b"}, 3600))
        other_scan = client.post(
            "/api/scans",
            json={"lockfile": {"name": "bob", "lockfileVersion": 3, "packages": {"": {"name": "bob"}}}},
        ).json()["scan_id"]

        scans = client.get("/api/scans")
        assert [scan["scan_id"] for scan in scans.json()] == [other_scan]
        assert scans.headers["cache-control"] == "private, no-store"
        assert {value.strip() for value in scans.headers["vary"].split(",")} >= {"Authorization", "Cookie"}
        assert client.get(f"/api/scans/{own_scan}").status_code == 404

    app.dependency_overrides.clear()


def test_extension_github_session_hands_off_to_browser_once() -> None:
    github = configured_github()
    app.dependency_overrides[get_github_service] = lambda: github

    with TestClient(app, follow_redirects=False) as client:
        handoff = client.post(
            "/api/github/auth/extension",
            headers={"Authorization": "Bearer github-token"},
            json={"next": "/dashboard"},
        )
        assert handoff.status_code == 200
        callback_path = urlparse(handoff.json()["url"]).path + "?" + urlparse(handoff.json()["url"]).query

        callback = client.get(callback_path)
        assert callback.status_code == 307
        assert callback.headers["location"] == "http://localhost:3000/dashboard?connected=1"
        assert client.get("/api/github/auth/status").json()["login"] == "octocat"

        replay = client.get(callback_path)
        assert replay.headers["location"] == "http://localhost:3000/github?github_error=invalid_extension_handoff"

    app.dependency_overrides.pop(get_github_service, None)


def test_extension_github_session_requires_bearer_token() -> None:
    github = configured_github()
    app.dependency_overrides[get_github_service] = lambda: github
    with TestClient(app) as client:
        response = client.post("/api/github/auth/extension", json={"next": "/dashboard"})
    app.dependency_overrides.pop(get_github_service, None)

    assert response.status_code == 401
    assert response.json()["detail"] == "A GitHub bearer token is required"


def test_extension_github_bearer_does_not_require_shared_api_key(monkeypatch) -> None:
    github = configured_github()
    app.dependency_overrides[get_github_service] = lambda: github
    monkeypatch.setattr(settings, "api_key", SecretStr("deployment-key"))
    monkeypatch.setattr(settings, "github_client_id", "client-id")
    monkeypatch.setattr(settings, "github_client_secret", SecretStr("client-secret"))
    monkeypatch.setattr(settings, "github_session_secret", SecretStr("session-secret"))

    with TestClient(app) as client:
        response = client.post(
            "/api/github/auth/extension",
            headers={"Authorization": "Bearer github-token"},
            json={"next": "/dashboard"},
        )

    app.dependency_overrides.pop(get_github_service, None)
    assert response.status_code == 200


def test_github_scan_rejects_unsafe_lockfile_path() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/github/scans",
            json={"owner": "octocat", "repository": "private-app", "lockfile_path": "../package-lock.json"},
        )

    assert response.status_code == 422

def test_sign_in_returns_to_the_requested_page_and_reports_failures_there() -> None:
    github = configured_github()
    app.dependency_overrides[get_github_service] = lambda: github

    with TestClient(app, follow_redirects=False) as client:
        login = client.get("/api/github/auth/login", params={"next": "/dashboard"})
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        callback = client.get("/api/github/auth/callback", params={"code": "oauth-code", "state": state})
        assert callback.headers["location"] == "http://localhost:3000/dashboard?connected=1"

        client.get("/api/github/auth/login", params={"next": "//evil.example"})
        denied = client.get("/api/github/auth/callback", params={"error": "access_denied"})
        assert denied.headers["location"] == "http://localhost:3000/github?github_error=access_denied"
    app.dependency_overrides.clear()
