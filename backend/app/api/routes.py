import hmac
import json
import secrets
import threading
import time
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app import __version__
from app.detection.ai_ranker import GeminiFindingRanker
from app.detection.osv import OsvClient
from app.core.config import settings
from app.core.security import limit_submissions
from app.gate import TrustGate
from app.github import GitHubApiError, GitHubService
from app.llm.openrouter import OpenRouterSimplifier
from app.models.database import ScanRepository
from app.blockchain.registry import ReleaseRegistryReader
from app.blockchain.verify import ReleaseVerifier
from app.graph.comparison import compare_graphs
from app.models.schemas import (
    DependencyFileScanRequest,
    ErrorResponse,
    Finding,
    GateReport,
    GateRequest,
    GitHubAuthStatus,
    GitHubExtensionHandoffRequest,
    GitHubExtensionHandoffResponse,
    GitHubRepository,
    GitHubScanRequest,
    HealthResponse,
    ScanAccepted,
    ScanComparisonResponse,
    ScanDetail,
    ScanGraph,
    ScanRequest,
    SimplifiedFindingResponse,
    SimplifiedScanResponse,
    SimplifyDirectFindingRequest,
    SimplifyFindingRequest,
    SimplifyScanRequest,
    VerificationResult,
    VerifyRequest,
    VersionChangeResponse,
)
from app.parsers.ecosystems import AUXILIARY_FILES, normalize_dependency_file
from app.parsers.npm_registry import NpmRegistryClient
from app.services import ScanService

router = APIRouter()
GITHUB_SESSION_COOKIE = "provenyx_github_session"
GITHUB_STATE_COOKIE = "provenyx_github_oauth_state"
GITHUB_SESSION_MAX_AGE = 8 * 60 * 60
GITHUB_STATE_MAX_AGE = 10 * 60
GITHUB_EXTENSION_HANDOFF_MAX_AGE = 60
LOCAL_SCAN_OWNER = "local-development"
_github_extension_handoffs: dict[str, tuple[str, str, str, float]] = {}
_github_extension_handoffs_lock = threading.Lock()
finding_ranker = (
    GeminiFindingRanker(
        api_key=settings.gemini_api_key.get_secret_value(),
        model=settings.gemini_model,
        base_url=settings.gemini_base_url,
        timeout_seconds=settings.gemini_timeout_seconds,
    )
    if settings.ai_ranking_enabled and settings.gemini_api_key is not None
    else None
)
openrouter_simplifier = OpenRouterSimplifier(
    api_keys=settings.openrouter_key_list,
    model=settings.openrouter_model,
    base_url=settings.openrouter_base_url,
    site_url=settings.openrouter_site_url,
    app_name=settings.openrouter_app_name,
    timeout_seconds=settings.openrouter_timeout_seconds,
)
scan_service = ScanService(
    ScanRepository(settings.database_url),
    NpmRegistryClient(
        base_url=settings.npm_registry_url,
        concurrency=settings.npm_registry_concurrency,
        timeout_seconds=settings.npm_registry_timeout_seconds,
        retries=settings.npm_registry_retries,
    ),
    osv=OsvClient(
        base_url=settings.osv_api_url,
        concurrency=settings.osv_concurrency,
        timeout_seconds=settings.osv_timeout_seconds,
        retries=settings.osv_retries,
    ),
    finding_ranker=finding_ranker,
    llm_simplifier=openrouter_simplifier,
)
github_service = GitHubService(settings)


release_verifier = ReleaseVerifier(
    ReleaseRegistryReader(
        rpc_url=settings.registry_rpc_url,
        deployment_file=settings.registry_deployment_file,
        address=settings.registry_address,
        timeout_seconds=settings.registry_timeout_seconds,
    ),
    scan_service.registry,
)


trust_gate = TrustGate(scan_service.registry, osv=scan_service.osv, verifier=release_verifier)


def get_scan_service() -> ScanService:
    return scan_service


def get_trust_gate() -> TrustGate:
    return trust_gate


def get_release_verifier() -> ReleaseVerifier:
    return release_verifier


def get_github_service() -> GitHubService:
    return github_service


def get_openrouter_simplifier() -> OpenRouterSimplifier:
    return openrouter_simplifier


def _github_token(request: Request, github: GitHubService) -> str:
    session = github.codec.decode(request.cookies.get(GITHUB_SESSION_COOKIE)) if github.configured else None
    token = session.get("token") if session else None
    if not isinstance(token, str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Connect a GitHub account first")
    return token


async def _github_credentials(request: Request, github: GitHubService) -> tuple[str, str]:
    if not github.configured:
        return "", LOCAL_SCAN_OWNER
    session = github.codec.decode(request.cookies.get(GITHUB_SESSION_COOKIE))
    token = session.get("token") if session else None
    subject = session.get("subject") if session else None
    if not isinstance(token, str):
        token = _github_bearer_token(request)
    if isinstance(subject, str):
        return token, subject
    try:
        return token, github.subject(await github.get_user(token))
    except GitHubApiError as exc:
        raise _github_error(exc)


def _github_error(exc: GitHubApiError) -> HTTPException:
    code = exc.status_code if exc.status_code in (401, 403, 404, 413, 422) else 502
    return HTTPException(code, exc.detail)


def _github_bearer_token(request: Request) -> str:
    scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "A GitHub bearer token is required")
    return token


def _create_github_extension_handoff(token: str, subject: str, next_path: str) -> str:
    now = time.monotonic()
    code = secrets.token_urlsafe(32)
    with _github_extension_handoffs_lock:
        expired = [key for key, value in _github_extension_handoffs.items() if value[3] <= now]
        for key in expired:
            del _github_extension_handoffs[key]
        _github_extension_handoffs[code] = (
            token,
            subject,
            safe_next_path(next_path),
            now + GITHUB_EXTENSION_HANDOFF_MAX_AGE,
        )
    return code


def _consume_github_extension_handoff(code: str) -> tuple[str, str, str] | None:
    with _github_extension_handoffs_lock:
        value = _github_extension_handoffs.pop(code, None)
    if value is None or value[3] <= time.monotonic():
        return None
    return value[0], value[1], value[2]


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="provenyx-api", version=__version__)


@router.get("/api/github/auth/status", response_model=GitHubAuthStatus)
async def github_auth_status(
    request: Request,
    response: Response,
    github: Annotated[GitHubService, Depends(get_github_service)],
) -> GitHubAuthStatus:
    if not github.configured:
        return GitHubAuthStatus(configured=False, connected=False)
    session = github.codec.decode(request.cookies.get(GITHUB_SESSION_COOKIE))
    token = session.get("token") if session else None
    if not isinstance(token, str):
        return GitHubAuthStatus(configured=True, connected=False)
    try:
        user = await github.get_user(token)
    except GitHubApiError:
        response.delete_cookie(GITHUB_SESSION_COOKIE)
        return GitHubAuthStatus(configured=True, connected=False)
    return GitHubAuthStatus(
        configured=True,
        connected=True,
        login=user.get("login"),
        avatar_url=user.get("avatar_url"),
    )


DEFAULT_AFTER_LOGIN = "/github"


def safe_next_path(value: str | None) -> str:
    """A same-site path to return to after sign-in. Anything else falls back to the default, so the
    login link cannot be used as an open redirect (e.g. next=//evil.example or next=https://…)."""
    if value and value.startswith("/") and not value.startswith(("//", "/\\")) and "\\" not in value and len(value) <= 512:
        return value
    return DEFAULT_AFTER_LOGIN


def _frontend_redirect(path: str, **query: str) -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{settings.frontend_url.rstrip('/')}{path}{separator}{urlencode(query)}")


@router.post("/api/github/auth/extension", response_model=GitHubExtensionHandoffResponse)
async def github_extension_handoff(
    body: GitHubExtensionHandoffRequest,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
) -> GitHubExtensionHandoffResponse:
    if not github.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub authentication is not configured")
    token = _github_bearer_token(request)
    try:
        user = await github.get_user(token)
    except GitHubApiError as exc:
        raise _github_error(exc)
    code = _create_github_extension_handoff(token, github.subject(user), body.next)
    query = urlencode({"code": code})
    return GitHubExtensionHandoffResponse(
        url=f"{settings.frontend_url.rstrip('/')}/api/github/auth/extension/callback?{query}"
    )


@router.get("/api/github/auth/extension/callback")
def github_extension_callback(
    github: Annotated[GitHubService, Depends(get_github_service)],
    code: str = "",
) -> RedirectResponse:
    handoff = _consume_github_extension_handoff(code)
    if handoff is None:
        return _frontend_redirect(DEFAULT_AFTER_LOGIN, github_error="invalid_extension_handoff")
    token, subject, next_path = handoff
    response = _frontend_redirect(next_path, connected="1")
    response.set_cookie(
        GITHUB_SESSION_COOKIE,
        github.codec.encode({"token": token, "subject": subject}, GITHUB_SESSION_MAX_AGE),
        max_age=GITHUB_SESSION_MAX_AGE,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
    )
    return response


@router.get("/api/github/auth/login")
def github_auth_login(
    github: Annotated[GitHubService, Depends(get_github_service)],
    next: str | None = None,
) -> RedirectResponse:
    if not github.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub authentication is not configured")
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(github.authorization_url(state))
    response.set_cookie(
        GITHUB_STATE_COOKIE,
        # `next` rides in the encrypted state cookie, so the callback cannot be pointed elsewhere.
        github.codec.encode({"state": state, "next": safe_next_path(next)}, GITHUB_STATE_MAX_AGE),
        max_age=GITHUB_STATE_MAX_AGE,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
    )
    return response


@router.get("/api/github/auth/callback")
async def github_auth_callback(
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    state_payload = github.codec.decode(request.cookies.get(GITHUB_STATE_COOKIE)) if github.configured else None
    expected_state = state_payload.get("state") if state_payload else None
    after_login = safe_next_path(state_payload.get("next") if state_payload else None)
    if error or not code or not state or not isinstance(expected_state, str) or not hmac.compare_digest(state, expected_state):
        return _frontend_redirect(after_login, github_error=error or "invalid_oauth_state")
    try:
        token = await github.exchange_code(code)
        user = await github.get_user(token)
    except GitHubApiError as exc:
        return _frontend_redirect(after_login, github_error=exc.detail)
    response = _frontend_redirect(after_login, connected="1")
    response.delete_cookie(GITHUB_STATE_COOKIE)
    response.set_cookie(
        GITHUB_SESSION_COOKIE,
        github.codec.encode({"token": token, "subject": github.subject(user)}, GITHUB_SESSION_MAX_AGE),
        max_age=GITHUB_SESSION_MAX_AGE,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
    )
    return response


@router.post("/api/github/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def github_auth_logout(response: Response) -> None:
    response.delete_cookie(GITHUB_SESSION_COOKIE)


@router.get("/api/github/repositories", response_model=list[GitHubRepository])
async def github_repositories(
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
) -> list[GitHubRepository]:
    try:
        return await github.list_repositories(_github_token(request, github))
    except GitHubApiError as exc:
        raise _github_error(exc)


@router.get("/api/github/repositories/{owner}/{repository}/lockfiles", response_model=list[str])
async def github_repository_lockfiles(
    owner: str,
    repository: str,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
) -> list[str]:
    try:
        return await github.find_lockfiles(_github_token(request, github), owner, repository)
    except GitHubApiError as exc:
        raise _github_error(exc)


@router.post(
    "/api/github/scans",
    response_model=ScanAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_submissions)],
)
async def create_github_scan(
    body: GitHubScanRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
) -> ScanAccepted:
    token, owner_id = await _github_credentials(request, github)
    lockfile_name = body.lockfile_path.rsplit("/", 1)[-1]
    lockfile_directory = body.lockfile_path.rsplit("/", 1)[0] if "/" in body.lockfile_path else "."
    try:
        content = await github.fetch_lockfile(
            token, body.owner, body.repository, body.lockfile_path, settings.max_scan_bytes
        )
    except GitHubApiError as exc:
        raise _github_error(exc)
    try:
        auxiliaries: dict[str, bytes] = {}
        for auxiliary_name in AUXILIARY_FILES.get(lockfile_name, ()):
            manifest_path = (
                f"{lockfile_directory}/{auxiliary_name}" if lockfile_directory != "." else auxiliary_name
            )
            try:
                auxiliaries[auxiliary_name] = await github.fetch_lockfile(
                    token, body.owner, body.repository, manifest_path, settings.max_scan_bytes
                )
            except GitHubApiError:
                # Adjacent manifests improve directness but are not required to scan exact releases.
                pass
        raw_lockfile = normalize_dependency_file(lockfile_name, content, auxiliaries)
        raw_lockfile["provenyxComponentPath"] = lockfile_directory
        lockfile = ScanRequest.model_validate(
            {
                "lockfile": raw_lockfile,
                "project_name": f"{body.owner}/{body.repository}",
                "client": "github/0.1.0",
            }
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Repository {lockfile_name} is invalid") from exc
    scan_id = service.create_scan(lockfile.lockfile.model_dump(mode="json"), lockfile.project_name, owner_id)
    background_tasks.add_task(service.analyze_scan, scan_id)
    return ScanAccepted(
        scan_id=scan_id,
        status="accepted",
        message="GitHub lockfile received. Dependency analysis has started.",
    )


@router.post(
    "/api/dependency-scans",
    response_model=ScanAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}},
    dependencies=[Depends(limit_submissions)],
)
async def create_dependency_file_scan(
    request: Request,
    background_tasks: BackgroundTasks,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
) -> ScanAccepted:
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
    submission = _validate(
        DependencyFileScanRequest,
        await _read_capped_body(request, 2 * settings.max_scan_bytes),
    )
    try:
        snapshot = normalize_dependency_file(submission.filename, submission.content, submission.auxiliaries)
        snapshot["provenyxComponentPath"] = submission.component_path
        validated = ScanRequest.model_validate(
            {
                "lockfile": snapshot,
                "project_name": submission.project_name or snapshot.get("name"),
                "client": submission.client,
            }
        )
    except (UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{submission.filename} is invalid: {exc}",
        ) from exc
    _, owner_id = await _github_credentials(request, github)
    scan_id = service.create_scan(validated.lockfile.model_dump(mode="json"), validated.project_name, owner_id)
    background_tasks.add_task(service.analyze_scan, scan_id)
    return ScanAccepted(
        scan_id=scan_id,
        status="accepted",
        message="Dependency file received. Dependency analysis has started.",
    )


@router.post(
    "/api/scans",
    response_model=ScanAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}},
    dependencies=[Depends(limit_submissions)],
)
async def create_scan(
    request: Request,
    background_tasks: BackgroundTasks,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
) -> ScanAccepted:
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")

    scan_request = _validate(ScanRequest, await _read_capped_body(request, settings.max_scan_bytes))

    _, owner_id = await _github_credentials(request, github)
    scan_id = service.create_scan(scan_request.lockfile.model_dump(mode="json"), scan_request.project_name, owner_id)
    background_tasks.add_task(service.analyze_scan, scan_id)
    return ScanAccepted(
        scan_id=scan_id,
        status="accepted",
        message="Lockfile received. Dependency analysis has started.",
    )


@router.post(
    "/api/gate",
    response_model=GateReport,
    responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}},
    dependencies=[Depends(limit_submissions)],
)
async def evaluate_trust_gate(
    request: Request,
    gate: Annotated[TrustGate, Depends(get_trust_gate)],
) -> GateReport:
    """Pull request trust gate: examines only releases the head lockfile adds or updates relative to the base."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
    gate_request = _validate(GateRequest, await _read_capped_body(request, 2 * settings.max_scan_bytes))
    return await gate.evaluate(
        gate_request.head_lockfile.model_dump(mode="json"),
        gate_request.base_lockfile.model_dump(mode="json") if gate_request.base_lockfile else None,
        fail_on=gate_request.fail_on,
        label=gate_request.label,
    )


async def _read_capped_body(request: Request, limit: int) -> bytes:
    """Read the body ourselves so the size cap also holds for chunked uploads without Content-Length."""
    too_large = HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"Request body exceeds {limit} bytes")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large
    return bytes(body)


def _validate(model, body: bytes):
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_context=False, include_input=False))


@router.get("/api/scans/{scan_id}", response_model=ScanDetail)
async def get_scan(
    scan_id: str,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
) -> ScanDetail:
    _, owner_id = await _github_credentials(request, github)
    row = service.repository.get_owned(scan_id, owner_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found")
    return ScanDetail(
        scan_id=row.scan_id,
        status=row.status,
        created_at=row.created_at,
        project_name=row.project_name,
        ecosystem=row.ecosystem,
        package_manager=row.package_manager,
        component_path=row.component_path,
        summary=row.summary,
        simplified=row.simplified,
        warnings=row.warnings,
        error=row.error,
    )


@router.get("/api/scans", response_model=list[ScanDetail])
async def list_scans(
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ScanDetail]:
    _, owner_id = await _github_credentials(request, github)
    return [
        ScanDetail(
            scan_id=row.scan_id,
            status=row.status,
            created_at=row.created_at,
            project_name=row.project_name,
            ecosystem=row.ecosystem,
            package_manager=row.package_manager,
            component_path=row.component_path,
            summary=row.summary,
            simplified=row.simplified,
            warnings=row.warnings,
            error=row.error,
        )
        for row in service.repository.list_recent(owner_id, limit, offset)
    ]


@router.get("/api/scans/{scan_id}/graph", response_model=ScanGraph)
async def get_scan_graph(
    scan_id: str,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
) -> ScanGraph:
    _, owner_id = await _github_credentials(request, github)
    row = service.repository.get_owned(scan_id, owner_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found")
    if row.graph is None:
        return ScanGraph(
            scan_id=row.scan_id,
            status=row.status,
            ecosystem=row.ecosystem,
            package_manager=row.package_manager,
            component_path=row.component_path,
            warnings=row.warnings,
        )
    return ScanGraph.model_validate(row.graph)


@router.get("/api/scans/{scan_id}/findings", response_model=list[Finding])
async def get_scan_findings(
    scan_id: str,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
) -> list[Finding]:
    _, owner_id = await _github_credentials(request, github)
    if service.repository.get_owned(scan_id, owner_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found")
    return [Finding.model_validate(finding) for finding in service.repository.get_findings(scan_id)]


@router.get(
    "/api/scans/{scan_id}/compare/{baseline_scan_id}",
    response_model=ScanComparisonResponse,
)
async def compare_scans(
    scan_id: str,
    baseline_scan_id: str,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
) -> ScanComparisonResponse:
    _, owner_id = await _github_credentials(request, github)
    try:
        return service.compare_scans(baseline_scan_id, scan_id, owner_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found")
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.post("/api/releases/verify", response_model=VerificationResult)
async def verify_release(
    body: VerifyRequest,
    verifier: Annotated[ReleaseVerifier, Depends(get_release_verifier)],
) -> VerificationResult:
    return await verifier.verify(body.package, body.version)


async def _execute_scan_simplification(
    scan_id: str,
    target_language: str,
    request: Request,
    github: GitHubService,
    service: ScanService,
    simplifier: OpenRouterSimplifier,
) -> SimplifiedScanResponse:
    _, owner_id = await _github_credentials(request, github)
    row = service.repository.get_owned(scan_id, owner_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found")
    if row.status != "complete":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Scan is {row.status}, must be complete")

    findings = [Finding.model_validate(f) for f in service.repository.get_findings(scan_id)]

    comparison: ScanComparisonResponse | None = None
    if row.project_name and row.graph:
        baseline_row = service.repository.find_latest_complete(
            row.project_name,
            owner_id=row.owner_id,
            before=row.created_at,
            exclude_scan_id=scan_id,
            ecosystem=row.ecosystem,
            package_manager=row.package_manager,
            component_path=row.component_path,
        )
        if baseline_row is not None and baseline_row.graph is not None:
            try:
                raw_comp = compare_graphs(
                    ScanGraph.model_validate(baseline_row.graph),
                    ScanGraph.model_validate(row.graph),
                )
                comparison = ScanComparisonResponse(
                    baseline_scan_id=baseline_row.scan_id,
                    current_scan_id=scan_id,
                    added_packages=list(raw_comp.added_packages),
                    removed_packages=list(raw_comp.removed_packages),
                    version_changes=[
                        VersionChangeResponse(
                            name=c.name,
                            from_versions=list(c.from_versions),
                            to_versions=list(c.to_versions),
                        )
                        for c in raw_comp.version_changes
                    ],
                    added_edges=list(raw_comp.added_edges),
                )
            except Exception:
                comparison = None

    analysis, provider = await simplifier.simplify_scan_output(
        project_name=row.project_name,
        summary=row.summary,
        findings=findings,
        comparison=comparison,
        language=target_language,
    )

    return SimplifiedScanResponse(
        scan_id=scan_id,
        language=target_language,
        headline=analysis.headline,
        summary=analysis.summary,
        risk_level=analysis.risk_level,
        key_points=analysis.key_points,
        simplified_findings=analysis.simplified_findings,
        action_items=analysis.action_items,
        provider=provider,
        model=simplifier.model_name,
    )


async def _execute_finding_simplification(
    scan_id: str,
    finding_id: str,
    target_language: str,
    request: Request,
    github: GitHubService,
    service: ScanService,
    simplifier: OpenRouterSimplifier,
) -> SimplifiedFindingResponse:
    _, owner_id = await _github_credentials(request, github)
    if service.repository.get_owned(scan_id, owner_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found")
    findings = service.repository.get_findings(scan_id)
    target = next((f for f in findings if f.get("id") == finding_id), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Finding not found")
    finding_model = Finding.model_validate(target)
    simplified_item, provider = await simplifier.simplify_single_finding(finding_model, language=target_language)
    return SimplifiedFindingResponse(
        finding_id=finding_id,
        language=target_language,
        title=simplified_item.title,
        what_happened=simplified_item.what_happened,
        why_it_matters=simplified_item.why_it_matters,
        recommendation=simplified_item.recommendation,
        provider=provider,
        model=simplifier.model_name,
    )


@router.get("/api/scans/{scan_id}/simplify", response_model=SimplifiedScanResponse)
async def get_scan_simplified(
    scan_id: str,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
    simplifier: Annotated[OpenRouterSimplifier, Depends(get_openrouter_simplifier)],
    language: Annotated[str, Query(max_length=64)] = "English",
) -> SimplifiedScanResponse:
    return await _execute_scan_simplification(scan_id, language, request, github, service, simplifier)


@router.post("/api/scans/{scan_id}/simplify", response_model=SimplifiedScanResponse)
async def post_scan_simplified(
    scan_id: str,
    body: SimplifyScanRequest,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
    simplifier: Annotated[OpenRouterSimplifier, Depends(get_openrouter_simplifier)],
) -> SimplifiedScanResponse:
    return await _execute_scan_simplification(scan_id, body.language, request, github, service, simplifier)


@router.get("/api/scans/{scan_id}/findings/{finding_id}/simplify", response_model=SimplifiedFindingResponse)
async def get_finding_simplified(
    scan_id: str,
    finding_id: str,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
    simplifier: Annotated[OpenRouterSimplifier, Depends(get_openrouter_simplifier)],
    language: Annotated[str, Query(max_length=64)] = "English",
) -> SimplifiedFindingResponse:
    return await _execute_finding_simplification(scan_id, finding_id, language, request, github, service, simplifier)


@router.post("/api/scans/{scan_id}/findings/{finding_id}/simplify", response_model=SimplifiedFindingResponse)
async def post_finding_simplified(
    scan_id: str,
    finding_id: str,
    body: SimplifyFindingRequest,
    request: Request,
    github: Annotated[GitHubService, Depends(get_github_service)],
    service: Annotated[ScanService, Depends(get_scan_service)],
    simplifier: Annotated[OpenRouterSimplifier, Depends(get_openrouter_simplifier)],
) -> SimplifiedFindingResponse:
    return await _execute_finding_simplification(scan_id, finding_id, body.language, request, github, service, simplifier)


@router.post("/api/findings/simplify", response_model=SimplifiedFindingResponse)
async def post_direct_finding_simplified(
    body: SimplifyDirectFindingRequest,
    simplifier: Annotated[OpenRouterSimplifier, Depends(get_openrouter_simplifier)],
) -> SimplifiedFindingResponse:
    item, provider = await simplifier.simplify_single_finding(body.finding, language=body.language)
    return SimplifiedFindingResponse(
        finding_id=body.finding.id,
        language=body.language,
        title=item.title,
        what_happened=item.what_happened,
        why_it_matters=item.why_it_matters,
        recommendation=item.recommendation,
        provider=provider,
        model=simplifier.model_name,
    )

