// Mirrors docs/api-contract.md. Keep in sync with backend/app/models/schemas.py and extension/src/types.

export type ScanStatus = 'accepted' | 'analyzing' | 'complete' | 'failed'
export type Severity = 'info' | 'low' | 'medium' | 'high'
export type Confidence = 'low' | 'medium' | 'high'

export interface ScanAccepted {
  scan_id: string
  status: 'accepted'
  message: string
}

export interface GitHubAuthStatus {
  configured: boolean
  connected: boolean
  login: string | null
  avatar_url: string | null
}

export interface GitHubRepository {
  id: number
  name: string
  full_name: string
  private: boolean
  description: string | null
  default_branch: string
  html_url: string
  updated_at: string
  owner_avatar_url: string | null
}

export interface ScanSummary {
  packages: number
  direct: number
  transitive: number
  findings: number
}

export interface SimplifiedFindingItem {
  finding_id: string
  package: string
  version: string
  title: string
  what_happened: string
  why_it_matters: string
  recommendation: string
}

export interface SimplifiedScanResponse {
  scan_id: string
  language: string
  headline: string
  summary: string
  risk_level: string
  key_points: string[]
  simplified_findings: SimplifiedFindingItem[]
  action_items: string[]
  provider: 'openrouter' | 'fallback'
  model: string
}

export interface SimplifiedFindingResponse {
  finding_id: string
  language: string
  title: string
  what_happened: string
  why_it_matters: string
  recommendation: string
  provider: 'openrouter' | 'fallback'
  model: string
}

export interface Scan {
  scan_id: string
  status: ScanStatus
  created_at: string
  project_name: string | null
  ecosystem: string
  package_manager: string
  component_path: string
  error: string | null
  summary: ScanSummary | null
  simplified?: SimplifiedScanResponse | null
  warnings?: string[]
}

export interface Evidence {
  source: string
  field: string
  value: unknown
  observed_at: string | null
}

export interface Finding {
  id: string
  rule_id: string
  package: string
  version: string
  severity: Severity
  confidence: Confidence
  /** 0-100, deterministic investigation priority (higher first). */
  priority_score: number
  ranking_source?: 'deterministic' | 'gemini'
  ai_priority_score?: number | null
  ai_rationale?: string | null
  title: string
  explanation: string
  evidence: Evidence[]
  unknowns?: string[]
  /** Each path starts with the literal "root" (the scanned project), then node ids "name@version". */
  paths: string[][]
}

export interface RegistryMetadata {
  status: 'available' | 'unavailable'
  publisher: string | null
  description: string | null
  license: string | null
  deprecated: string | null
  published_at: string | null
  dist_integrity: string | null
  error: string | null
}

export interface GraphNode {
  id: string
  name: string
  version: string
  purl?: string | null
  direct: boolean
  dev?: boolean
  lockfile_paths: string[]
  resolved_urls?: string[]
  lockfile_integrities?: string[]
  registry?: RegistryMetadata
}

export interface GraphEdge {
  from: string
  to: string
  type: 'prod' | 'dev' | 'optional' | 'peer'
}

/** The project itself is not a node; findings refer to it as "root" (see ROOT_ID). */
export interface DependencyGraphData {
  scan_id: string
  status?: ScanStatus
  ecosystem?: string
  package_manager?: string
  component_path?: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  warnings?: string[]
}

export const ROOT_ID = 'root'

export type VerificationStatusKind = 'match' | 'mismatch' | 'no_record' | 'unavailable'

export interface VerificationResult {
  package: string
  version: string
  release_id: string
  status: VerificationStatusKind
  manifest_hash: string | null
  anchored_hash: string | null
  anchored_at: string | null
  manifest: Record<string, unknown> | null
  registry: { network: string; chain_id: number; address: string } | null
  detail: string | null
}
