// Mirrors docs/api-contract.md and backend/app/models/schemas.py. Keep them in sync.

export interface HealthResponse {
  status: 'ok';
  service: string;
  version: string;
}

/** Only the fields Provenyx inspects; everything else in the lockfile is passed through untouched. */
export interface Lockfile {
  name?: string;
  version?: string;
  lockfileVersion: number;
  packages?: Record<string, unknown>;
  dependencies?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ScanRequest {
  lockfile: Lockfile;
  project_name?: string;
  client?: string;
}

export interface ScanAccepted {
  scan_id: string;
  status: 'accepted';
  message: string;
}

export type ScanStatus = 'accepted' | 'analyzing' | 'complete' | 'failed';

export interface Scan {
  scan_id: string;
  status: ScanStatus;
  created_at: string;
  project_name: string | null;
  ecosystem: string;
  package_manager: string;
  component_path: string;
  error: string | null;
  summary: { packages: number; direct: number; transitive: number; findings: number } | null;
  warnings?: string[];
}

export type Severity = 'info' | 'low' | 'medium' | 'high';

export interface Finding {
  id: string;
  rule_id: string;
  package: string;
  version: string;
  severity: Severity;
  confidence: 'low' | 'medium' | 'high';
  /** 0–100, deterministic investigation priority (higher first). */
  priority_score: number;
  ranking_source?: 'deterministic' | 'gemini';
  ai_priority_score?: number | null;
  ai_rationale?: string | null;
  title: string;
  explanation: string;
  evidence: { source: string; field: string; value: unknown; observed_at: string | null }[];
  unknowns?: string[];
  /** Each path starts with the literal "root" (the scanned project), then node ids "name@version". */
  paths: string[][];
}

export type BackendState = 'unknown' | 'connected' | 'unreachable';

export interface SimplifiedFindingItem {
  finding_id: string;
  package: string;
  version: string;
  title: string;
  what_happened: string;
  why_it_matters: string;
  recommendation: string;
}

export interface SimplifiedScanResponse {
  scan_id: string;
  language: string;
  headline: string;
  summary: string;
  risk_level: string;
  key_points: string[];
  simplified_findings: SimplifiedFindingItem[];
  action_items: string[];
  provider: 'openrouter' | 'fallback';
  model: string;
}

/** `findings` is undefined while not known, e.g. when the API does not provide findings yet. */
export type LastScan =
  | { kind: 'none' }
  | { kind: 'in-progress'; scanId: string; status: ScanStatus; message: string; at: Date }
  | { kind: 'complete'; scan: Scan; findings?: Finding[]; at: Date }
  | { kind: 'failed'; scanId?: string; message: string; at: Date };
