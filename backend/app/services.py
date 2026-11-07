from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from typing import Any

from app.detection.ai_ranker import AiRankingError, GeminiFindingRanker
from app.detection.osv import OsvClient
from app.graph.builder import build_dependency_graph
from app.graph.comparison import compare_graphs
from app.llm.openrouter import OpenRouterSimplifier
from app.detection.rules import (
    detect_graph_changes,
    detect_install_script_changes,
    detect_known_vulnerabilities,
    detect_provenance_regressions,
    detect_publisher_changes,
    detect_unusual_release_timing,
)
from app.models.database import ScanRepository
from app.models.schemas import (
    GraphEdge,
    GraphNode,
    RegistryMetadataResponse,
    ScanComparisonResponse,
    ScanGraph,
    ScanSummary,
    VersionChangeResponse,
)
from app.parsers.lockfile import ParsedLockfile, ParsedPackage, package_purl, parse_lockfile
from app.parsers.npm_registry import NpmRegistryClient, RegistryMetadata


logger = logging.getLogger("provenyx.scan")


class ScanService:
    def __init__(
        self,
        repository: ScanRepository,
        registry: NpmRegistryClient,
        *,
        osv: OsvClient | None = None,
        finding_ranker: GeminiFindingRanker | None = None,
        llm_simplifier: OpenRouterSimplifier | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.osv = osv
        self.finding_ranker = finding_ranker
        self.llm_simplifier = llm_simplifier

    def create_scan(self, lockfile: dict[str, Any], project_name: str | None, owner_id: str | None = None) -> str:
        scan_id = str(uuid.uuid4())
        parsed = parse_lockfile(lockfile)
        self.repository.create(
            scan_id,
            project_name or parsed.name,
            lockfile,
            owner_id,
            ecosystem=parsed.ecosystem,
            package_manager=parsed.package_manager,
            component_path=parsed.component_path,
        )
        return scan_id

    async def analyze_scan(self, scan_id: str) -> None:
        row = self.repository.get(scan_id)
        if row is None:
            return
        self.repository.set_analyzing(scan_id)
        try:
            parsed = parse_lockfile(row.lockfile)
            metadata = (
                await self.registry.fetch_many((package.name, package.version) for package in parsed.packages)
                if parsed.ecosystem == "npm"
                else {}
            )
            scan_graph = build_scan_graph(scan_id, parsed, metadata)
            analysis_warnings = list(scan_graph.warnings)
            if parsed.ecosystem != "npm":
                analysis_warnings.append(
                    f"{parsed.ecosystem} registry enrichment is not available; publisher, install-script, "
                    "provenance, and release-timing rules did not run."
                )
            nodes = scan_graph.nodes
            project_name = row.project_name or parsed.name
            findings = []
            if self.osv is not None:
                advisories = await self.osv.fetch_many(
                    ((node.name, node.version) for node in nodes), ecosystem=parsed.ecosystem
                )
                unavailable_count = sum(result is None for result in advisories.values())
                if unavailable_count:
                    analysis_warnings.append(
                        f"OSV advisory lookup was unavailable for {unavailable_count} package release(s); vulnerability status is unknown"
                    )
                findings.extend(
                    detect_known_vulnerabilities(scan_graph, advisories=advisories, observed_at=row.created_at)
                )
            baseline_row = (
                self.repository.find_latest_complete(
                    project_name,
                    owner_id=row.owner_id,
                    before=row.created_at,
                    exclude_scan_id=scan_id,
                    ecosystem=row.ecosystem,
                    package_manager=row.package_manager,
                    component_path=row.component_path,
                )
                if project_name
                else None
            )
            if not project_name:
                analysis_warnings.append(
                    "The lockfile has no project name, so no baseline was selected; change-based rules did not run."
                )
            elif baseline_row is None or baseline_row.graph is None:
                analysis_warnings.append(
                    f"No earlier completed scan of {project_name!r} exists, so change-based rules did not run; "
                    "scan again after a dependency update to compare."
                )
            if project_name:
                if baseline_row is not None and baseline_row.graph is not None:
                    baseline_graph = ScanGraph.model_validate(baseline_row.graph)
                    findings.extend(
                        detect_graph_changes(
                            baseline_graph,
                            scan_graph,
                            observed_at=row.created_at,
                        )
                    )
                    if parsed.ecosystem == "npm":
                        findings.extend(
                            detect_publisher_changes(
                                baseline_graph,
                                scan_graph,
                                observed_at=row.created_at,
                            )
                        )
                        findings.extend(
                            detect_install_script_changes(baseline_graph, scan_graph, observed_at=row.created_at)
                        )
                        findings.extend(
                            detect_provenance_regressions(baseline_graph, scan_graph, observed_at=row.created_at)
                        )
                        comparison = compare_graphs(baseline_graph, scan_graph)
                        release_times = await self.registry.fetch_release_times_many(
                            change.name for change in comparison.version_changes
                        )
                        findings.extend(
                            detect_unusual_release_timing(
                                baseline_graph,
                                scan_graph,
                                release_times=release_times,
                                observed_at=row.created_at,
                            )
                        )
                    else:
                        comparison = compare_graphs(baseline_graph, scan_graph)
            findings.sort(
                key=lambda finding: (-finding.priority_score, finding.rule_id, finding.package, finding.version)
            )
            if findings and self.finding_ranker is not None:
                try:
                    findings = await self.finding_ranker.rank(findings)
                except AiRankingError:
                    analysis_warnings.append("AI ranking was unavailable; deterministic priorities were retained")
            direct_count = sum(node.direct for node in nodes)
            simplified_payload = None
            if self.llm_simplifier is not None and self.llm_simplifier.is_configured:
                try:
                    analysis, provider = await self.llm_simplifier.simplify_scan_output(
                        project_name=project_name,
                        summary={
                            "packages": len(nodes),
                            "direct": direct_count,
                            "transitive": len(nodes) - direct_count,
                            "findings": len(findings),
                        },
                        findings=findings,
                        comparison=comparison if (baseline_row is not None and baseline_row.graph is not None) else None,
                    )
                    simplified_payload = {
                        "headline": analysis.headline,
                        "summary": analysis.summary,
                        "risk_level": analysis.risk_level,
                        "key_points": analysis.key_points,
                        "simplified_findings": [f.model_dump() for f in analysis.simplified_findings],
                        "action_items": analysis.action_items,
                        "provider": provider,
                        "model": self.llm_simplifier.model_name,
                    }
                except Exception as exc:
                    logger.warning("Auto-simplification of scan %s failed: %s", scan_id, exc)
            scan_graph = scan_graph.model_copy(update={"warnings": analysis_warnings})
            summary = ScanSummary(
                packages=len(nodes),
                direct=direct_count,
                transitive=len(nodes) - direct_count,
                findings=len(findings),
            )
            self.repository.set_complete(
                scan_id,
                project_name=project_name,
                summary=summary.model_dump(mode="json"),
                graph=scan_graph.model_dump(mode="json", by_alias=True),
                findings=[finding.model_dump(mode="json") for finding in findings],
                warnings=analysis_warnings,
                simplified=simplified_payload,
            )
        except Exception as exc:
            # Full detail goes to the server log; the API only exposes the exception type.
            logger.exception("Dependency analysis failed for scan %s", scan_id)
            self.repository.set_failed(scan_id, f"Dependency analysis failed: {type(exc).__name__}")

    async def recover_interrupted(self) -> list[str]:
        """Re-run scans a restart interrupted. Oldest first, so each finds its correct baseline."""
        scan_ids = self.repository.list_unfinished()
        for scan_id in scan_ids:
            logger.info("Resuming interrupted scan %s", scan_id)
            await self.analyze_scan(scan_id)
        return scan_ids

    def compare_scans(self, baseline_scan_id: str, current_scan_id: str, owner_id: str) -> ScanComparisonResponse:
        baseline_row = self.repository.get_owned(baseline_scan_id, owner_id)
        current_row = self.repository.get_owned(current_scan_id, owner_id)
        if baseline_row is None or current_row is None:
            raise KeyError("Scan not found")
        if baseline_row.graph is None or current_row.graph is None:
            raise ValueError("Both scans must be complete before comparison")
        comparison = compare_graphs(
            ScanGraph.model_validate(baseline_row.graph),
            ScanGraph.model_validate(current_row.graph),
        )
        return ScanComparisonResponse(
            baseline_scan_id=baseline_scan_id,
            current_scan_id=current_scan_id,
            added_packages=list(comparison.added_packages),
            removed_packages=list(comparison.removed_packages),
            version_changes=[
                VersionChangeResponse(
                    name=change.name,
                    from_versions=list(change.from_versions),
                    to_versions=list(change.to_versions),
                )
                for change in comparison.version_changes
            ],
            added_edges=list(comparison.added_edges),
        )


def build_scan_graph(
    scan_id: str,
    parsed: ParsedLockfile,
    metadata: dict[tuple[str, str], RegistryMetadata],
) -> ScanGraph:
    """Turn a parsed lockfile into the API graph. Releases missing from `metadata` are marked unavailable."""
    graph = build_dependency_graph(parsed)
    package_evidence = _package_evidence(parsed.packages)
    install_script_ids = {f"{package.name}@{package.version}" for package in parsed.packages if package.has_install_script}
    nodes = []
    for node in graph.nodes:
        registry_metadata = metadata.get((node.name, node.version))
        evidence = package_evidence[node.id]
        nodes.append(
            GraphNode(
                id=node.id,
                name=node.name,
                version=node.version,
                purl=package_purl(parsed.ecosystem, node.name, node.version),
                direct=node.direct,
                lockfile_paths=list(node.lockfile_paths),
                resolved_urls=sorted(evidence["resolved_urls"]),
                lockfile_integrities=sorted(evidence["lockfile_integrities"]),
                lockfile_install_script=node.id in install_script_ids,
                registry=(
                    RegistryMetadataResponse.model_validate(asdict(registry_metadata))
                    if registry_metadata is not None
                    else RegistryMetadataResponse(status="unavailable", error="Registry metadata was not fetched")
                ),
            )
        )
    edges = [GraphEdge(source=edge.source, target=edge.target, type=edge.dependency_type) for edge in graph.edges]
    return ScanGraph(
        scan_id=scan_id,
        status="complete",
        ecosystem=parsed.ecosystem,
        package_manager=parsed.package_manager,
        component_path=parsed.component_path,
        nodes=nodes,
        edges=edges,
        warnings=list(graph.warnings),
    )


def _package_evidence(packages: tuple[ParsedPackage, ...]) -> dict[str, dict[str, set[str]]]:
    evidence: dict[str, dict[str, set[str]]] = {}
    for package in packages:
        node_id = f"{package.name}@{package.version}"
        node_evidence = evidence.setdefault(node_id, {"resolved_urls": set(), "lockfile_integrities": set()})
        if package.resolved:
            node_evidence["resolved_urls"].add(package.resolved)
        if package.integrity:
            node_evidence["lockfile_integrities"].add(package.integrity)
    return evidence