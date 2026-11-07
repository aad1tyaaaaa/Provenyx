from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.parsers.ecosystems import SUPPORTED_DEPENDENCY_FILES

SUPPORTED_LOCKFILE_VERSIONS = (1, 2, 3)
SUPPORTED_LOCKFILE_NAMES = SUPPORTED_DEPENDENCY_FILES


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class Lockfile(BaseModel):
    """Minimal structural check of an npm package-lock.json. Contents are never executed."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    version: str | None = None
    lockfileVersion: int
    packages: dict[str, dict[str, Any]] | None = None
    dependencies: dict[str, dict[str, Any]] | None = None

    @model_validator(mode="after")
    def check_shape(self) -> "Lockfile":
        if self.lockfileVersion not in SUPPORTED_LOCKFILE_VERSIONS:
            raise ValueError(
                f"Unsupported lockfileVersion {self.lockfileVersion}; expected one of {SUPPORTED_LOCKFILE_VERSIONS}"
            )
        if self.lockfileVersion >= 2 and self.packages is None:
            raise ValueError("lockfileVersion 2/3 requires a 'packages' object")
        if self.lockfileVersion == 1 and self.dependencies is None:
            raise ValueError("lockfileVersion 1 requires a 'dependencies' object")
        return self


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lockfile: Lockfile
    project_name: str | None = Field(default=None, max_length=214)
    client: str | None = Field(default=None, max_length=64, description="e.g. 'vscode-extension/0.1.0'")


class DependencyFileScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=128)
    content: str
    auxiliaries: dict[str, str] = Field(default_factory=dict)
    project_name: str | None = Field(default=None, max_length=214)
    client: str | None = Field(default=None, max_length=64)
    component_path: str = Field(default=".", max_length=1024)

    @model_validator(mode="after")
    def check_file_names(self) -> "DependencyFileScanRequest":
        if self.filename not in SUPPORTED_DEPENDENCY_FILES:
            raise ValueError(f"filename must be one of: {', '.join(SUPPORTED_DEPENDENCY_FILES)}")
        if any(name not in {"package.json", "composer.json", "go.mod"} for name in self.auxiliaries):
            raise ValueError("unsupported auxiliary manifest")
        parts = self.component_path.split("/")
        if self.component_path.startswith("/") or any(part in ("", "..") for part in parts):
            raise ValueError("component_path must be repository-relative")
        return self


class ScanAccepted(BaseModel):
    scan_id: str
    status: Literal["accepted"]
    message: str


class GitHubAuthStatus(BaseModel):
    configured: bool
    connected: bool
    login: str | None = None
    avatar_url: str | None = None


class GitHubExtensionHandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next: str = Field(default="/dashboard", max_length=512)


class GitHubExtensionHandoffResponse(BaseModel):
    url: str


class GitHubRepository(BaseModel):
    id: int
    name: str
    full_name: str
    private: bool
    description: str | None = None
    default_branch: str
    html_url: str
    updated_at: datetime
    owner_avatar_url: str | None = None


class GitHubScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
    repository: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    lockfile_path: str = Field(default="package-lock.json", min_length=1, max_length=1024)

    @model_validator(mode="after")
    def check_lockfile_path(self) -> "GitHubScanRequest":
        parts = self.lockfile_path.split("/")
        if self.lockfile_path.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise ValueError("lockfile_path must be a repository-relative path")
        if parts[-1] not in SUPPORTED_LOCKFILE_NAMES:
            raise ValueError(f"lockfile_path must point to one of: {', '.join(SUPPORTED_LOCKFILE_NAMES)}")
        return self


class ScanSummary(BaseModel):
    packages: int
    direct: int
    transitive: int
    findings: int


class ScanDetail(BaseModel):
    scan_id: str
    status: Literal["accepted", "analyzing", "complete", "failed"]
    created_at: datetime
    project_name: str | None
    ecosystem: str = "npm"
    package_manager: str = "npm"
    component_path: str = "."
    summary: ScanSummary | None = None
    simplified: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class RegistryMetadataResponse(BaseModel):
    status: Literal["available", "unavailable"]
    publisher: str | None = None
    description: str | None = None
    license: str | None = None
    deprecated: str | None = None
    published_at: str | None = None
    dist_integrity: str | None = None
    install_scripts: list[str] | None = None
    has_provenance: bool | None = None
    error: str | None = None


class GraphNode(BaseModel):
    id: str
    name: str
    version: str
    purl: str | None = None
    direct: bool
    lockfile_paths: list[str]
    resolved_urls: list[str] = Field(default_factory=list)
    lockfile_integrities: list[str] = Field(default_factory=list)
    lockfile_install_script: bool = False
    registry: RegistryMetadataResponse


class GraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    type: Literal["prod", "dev", "optional", "peer"]


class ScanGraph(BaseModel):
    scan_id: str
    status: Literal["accepted", "analyzing", "complete", "failed"]
    ecosystem: str = "npm"
    package_manager: str = "npm"
    component_path: str = "."
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VersionChangeResponse(BaseModel):
    name: str
    from_versions: list[str]
    to_versions: list[str]


class ScanComparisonResponse(BaseModel):
    baseline_scan_id: str
    current_scan_id: str
    added_packages: list[str]
    removed_packages: list[str]
    version_changes: list[VersionChangeResponse]
    added_edges: list[GraphEdge]


class FindingEvidence(BaseModel):
    source: Literal["npm-registry", "lockfile", "osv", "registry-contract"]
    field: str
    value: Any
    observed_at: datetime


class Finding(BaseModel):
    id: str
    rule_id: str
    package: str
    version: str
    severity: Literal["info", "low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    priority_score: int = Field(ge=0, le=100)
    ranking_source: Literal["deterministic", "gemini"] = "deterministic"
    ai_priority_score: int | None = Field(default=None, ge=0, le=100)
    ai_rationale: str | None = None
    title: str
    explanation: str
    evidence: list[FindingEvidence] = Field(min_length=1)
    paths: list[list[str]] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str


NPM_NAME_PATTERN = r"^(@[A-Za-z0-9-~][A-Za-z0-9-._~]*/)?[A-Za-z0-9-~][A-Za-z0-9-._~]*$"


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: str = Field(min_length=1, max_length=214, pattern=NPM_NAME_PATTERN)
    version: str = Field(min_length=1, max_length=128, pattern=r"^[0-9A-Za-z.+-]+$")


class RegistryLocation(BaseModel):
    network: str
    chain_id: int
    address: str


class VerificationResult(BaseModel):
    package: str
    version: str
    release_id: str
    status: Literal["match", "mismatch", "no_record", "unavailable"]
    manifest_hash: str | None = None
    anchored_hash: str | None = None
    anchored_at: datetime | None = None
    manifest: dict[str, Any] | None = None
    registry: RegistryLocation | None = None
    detail: str | None = None


GateVerdict = Literal["pass", "warn", "fail"]
GateCheckStatus = Literal["pass", "warn", "fail", "info"]
GateThreshold = Literal["high", "medium", "low", "never"]


class GateRequest(BaseModel):
    """Compare a pull request's lockfile against its base. Package code is never downloaded or executed."""

    model_config = ConfigDict(extra="forbid")

    base_lockfile: Lockfile | None = Field(default=None, description="Omit when the PR adds the lockfile")
    head_lockfile: Lockfile
    fail_on: GateThreshold = "high"
    label: str | None = Field(default=None, max_length=1024, description="e.g. the lockfile path, shown in the report")


class GateCheck(BaseModel):
    status: GateCheckStatus
    label: str


class GateRelease(BaseModel):
    package: str
    version: str
    change: Literal["added", "updated"]
    from_versions: list[str] = Field(default_factory=list)
    direct: bool
    verdict: GateVerdict
    checks: list[GateCheck]
    findings: list[Finding] = Field(default_factory=list)
    anchor: VerificationResult | None = None
    blast_radius: list[str] = Field(default_factory=list)


class GateSummary(BaseModel):
    added: int
    updated: int
    removed: int
    failed: int
    warned: int


class GateReport(BaseModel):
    verdict: GateVerdict
    fail_on: GateThreshold
    label: str | None = None
    summary: GateSummary
    releases: list[GateRelease]
    removed_packages: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="Lockfile resolution notes about changed releases")
    markdown: str


class SimplifiedFindingItem(BaseModel):
    finding_id: str
    package: str
    version: str
    title: str
    what_happened: str
    why_it_matters: str
    recommendation: str


class SimplifiedScanResponse(BaseModel):
    scan_id: str
    language: str
    headline: str
    summary: str
    risk_level: str
    key_points: list[str] = Field(default_factory=list)
    simplified_findings: list[SimplifiedFindingItem] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    provider: Literal["openrouter", "fallback"] = "fallback"
    model: str


class SimplifyScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="English", max_length=64, description="Target understanding language for explanation")


class SimplifyFindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="English", max_length=64, description="Target understanding language for explanation")


class SimplifyDirectFindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding: Finding
    language: str = Field(default="English", max_length=64, description="Target understanding language for explanation")


class SimplifiedFindingResponse(BaseModel):
    finding_id: str
    language: str
    title: str
    what_happened: str
    why_it_matters: str
    recommendation: str
    provider: Literal["openrouter", "fallback"] = "fallback"
    model: str

