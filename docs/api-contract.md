# Provenyx API contract (v0.1.0)

Shared contract between the VS Code extension (`extension/src/types/index.ts`), the dashboard, and the backend (`backend/app/models/schemas.py`). Change all three together.

Base URL (development): `http://localhost:8000`. Interactive docs: `http://localhost:8000/docs`.

## `GET /health`

`200 OK`

```json
{ "status": "ok", "service": "provenyx-api", "version": "0.1.0" }
```

Sends and returns no project data. The extension calls it on activation and before every scan.

## `POST /api/scans`

Submit an npm lockfile or an ecosystem-normalized dependency snapshot produced by the extension. The API validates it, stores a local snapshot (SQLite), and starts dependency analysis in a background task. Poll `GET /api/scans/{scan_id}` for progress.

Request headers: `Content-Type: application/json` (otherwise `415`).

Request body (max 10 MiB, configurable with `PROVENYX_MAX_SCAN_BYTES`; otherwise `413`):

```json
{
  "lockfile": { "name": "my-app", "lockfileVersion": 3, "packages": { "": { "name": "my-app" } } },
  "project_name": "my-app",
  "client": "vscode-extension/0.1.0"
}
```

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `lockfile` | object | yes | Parsed `package-lock.json` or a normalized dependency snapshot. Unknown keys are allowed and passed through. |
| `lockfile.lockfileVersion` | integer | yes | `1`, `2` or `3`. |
| `lockfile.packages` | object | for v2/v3 | |
| `lockfile.dependencies` | object | for v1 | |
| `project_name` | string ≤ 214 | no | |
| `client` | string ≤ 64 | no | Identifies the caller. |

No other top-level fields are accepted.

`202 Accepted`

```json
{
  "scan_id": "3f1c9a52-6a77-4a8e-9d4e-1c0b2f6f9e10",
  "status": "accepted",
  "message": "Lockfile received. Dependency analysis has started."
}
```

## Errors

| Status | When | Body |
| --- | --- | --- |
| `413` | Body larger than the limit | `{ "detail": "Request body exceeds N bytes" }` |
| `415` | Content-Type is not `application/json` | `{ "detail": "Content-Type must be application/json" }` |
| `422` | Malformed JSON or schema violation | `{ "detail": [ { "type", "loc", "msg" } ] }` (FastAPI format; input values are not echoed) |

## CORS

Browser access is restricted to `PROVENYX_CORS_ORIGINS` (default `http://localhost:3000,http://127.0.0.1:3000`), methods `GET`/`POST`, header `Content-Type`, with credentialed requests enabled for the HTTP-only GitHub session cookie. The extension runs in Node and is not subject to CORS.

## GitHub project import

| Endpoint | Purpose |
| --- | --- |
| `GET /api/github/auth/status` | OAuth configuration and current connected user |
| `GET /api/github/auth/login` | Start GitHub OAuth authorization |
| `GET /api/github/auth/callback` | Validate OAuth state and create the encrypted session cookie |
| `POST /api/github/auth/logout` | Delete the GitHub session cookie |
| `GET /api/github/repositories` | List all repositories accessible through the connected account |
| `GET /api/github/repositories/{owner}/{repository}/lockfiles` | Discover root and nested supported dependency files on the default branch |
| `POST /api/github/scans` | Fetch and normalize one selected supported dependency file |
| `POST /api/dependency-scans` | Normalize and scan a manually uploaded native dependency file |

GitHub discovery supports the same filenames as the VS Code extension. `POST /api/github/scans` accepts `{ "owner": "org", "repository": "project", "lockfile_path": "path/to/requirements.txt" }`. It fetches only the selected dependency file and an optional adjacent manifest used for directness; it does not clone the repository, install dependencies, or execute project code. GitHub endpoints return `401` until an account is connected.

`POST /api/dependency-scans` accepts `{ "filename", "content", "auxiliaries", "project_name", "client", "component_path" }`. `auxiliaries` may contain `package.json`, `composer.json`, or `go.mod`; source files are rejected.

## Scan results (implemented — Phases 3–4, owner: Omkar)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/scans` | Recent persisted scans, newest first — a JSON **array** of Scan |
| `GET /api/scans/{scan_id}` | Status and summary |
| `GET /api/scans/{scan_id}/graph` | Dependency nodes and edges |
| `GET /api/scans/{scan_id}/findings` | Evidence-backed findings — a JSON **array** of Finding |
| `GET /api/scans/{scan_id}/compare/{baseline_scan_id}` | Additions, removals, upgrades, and new edges between two scans |
| `POST /api/releases/verify` | Release-record verification against the on-chain registry (Phase 6) |

Unknown `scan_id` → `404 { "detail": "Scan not found" }`. Clients treat `404`/`405`/`501` on these routes as "not available" rather than as an empty result.

### Scan — `GET /api/scans/{scan_id}`

```jsonc
{
  "scan_id": "uuid",
  "status": "complete",            // accepted | analyzing | complete | failed
  "created_at": "2026-10-12T10:00:00Z",
  "project_name": "my-app",        // or null
  "ecosystem": "npm",
  "package_manager": "npm",
  "component_path": ".",
  "summary": { "packages": 120, "direct": 8, "transitive": 112, "findings": 2 },   // null until complete
  "warnings": ["Could not resolve 'x' declared by y@1.0.0"],   // things the analysis could not determine
  "error": null                    // reason when status = failed
}
```

### Graph — `GET /api/scans/{scan_id}/graph`

Node `id` is `name@version`, so multiple installed versions stay distinct. **The project itself is not a node**; findings refer to it as `"root"`, and the UI draws it with edges to every `direct` node.

```jsonc
{
  "scan_id": "uuid",
  "status": "complete",
  "ecosystem": "npm",
  "package_manager": "npm",
  "component_path": ".",
  "nodes": [
    { "id": "ms@2.1.3", "name": "ms", "version": "2.1.3", "purl": "pkg:npm/ms@2.1.3", "direct": true,
      "lockfile_paths": ["node_modules/ms"],
      "resolved_urls": ["https://registry.npmjs.org/ms/-/ms-2.1.3.tgz"],
      "lockfile_integrities": ["sha512-…"],
      "registry": { "status": "available", "publisher": "release-owner", "description": "…", "license": "MIT", "deprecated": null,
                    "published_at": "2021-…", "dist_integrity": "sha512-…", "error": null } }
  ],
  "edges": [ { "from": "a@1.0.0", "to": "ms@2.1.3", "type": "prod" } ],   // prod | dev | optional | peer
  "warnings": []
}
```

`registry.status = "unavailable"` means npm metadata could not be fetched; every field is then unknown, never guessed.

### Findings — `GET /api/scans/{scan_id}/findings`

Every finding carries at least one piece of evidence. `severity`, `confidence`, and deterministic `priority_score` are independent. When optional Gemini ranking succeeds, `ai_priority_score` controls list order while both scores remain visible. Provider failure retains deterministic order.

Implemented rules are `known-vulnerability`, `new-dependency`, `dependency-expansion`, `unusual-release-timing`, and `publisher-change`. OSV checks exact npm package versions on every scan, including the first; change rules require a baseline. Publisher changes compare npm's release-specific `_npmUser.name` values and are omitted when either side is unavailable.

```jsonc
[
  {
    "id": "…",
    "rule_id": "new-dependency",
    "package": "example-pkg", "version": "1.2.0",
    "severity": "medium",          // info | low | medium | high
    "confidence": "high",          // low | medium | high
    "priority_score": 55,          // 0–100, deterministic
    "ranking_source": "gemini",   // deterministic | gemini
    "ai_priority_score": 72,       // null unless Gemini ranking succeeds
    "ai_rationale": "Brief reason without a compromise claim",
    "title": "Short, factual headline",
    "explanation": "Why this was flagged and what to check.",
    "evidence": [
      { "source": "lockfile", "field": "…", "value": "1.2.0", "observed_at": "2026-10-12T10:00:00Z" }
    ],                              // source: lockfile | npm-registry | osv | registry-contract
    "paths": [["root", "dep-a@2.0.0", "example-pkg@1.2.0"]]   // "root" = the project, then graph node ids
  }
]
```

### Comparison — `GET /api/scans/{scan_id}/compare/{baseline_scan_id}`

```jsonc
{ "baseline_scan_id": "uuid", "current_scan_id": "uuid",
  "added_packages": ["name@version"], "removed_packages": ["name@version"],
  "version_changes": [ { "name": "name", "from_versions": ["1.0.0"], "to_versions": ["2.0.0"] } ],
  "added_edges": [ { "from": "parent@version", "to": "child@version", "type": "prod" } ] }
```

`409` when either scan is not complete.

### Release verification — `POST /api/releases/verify` (implemented — Phase 6)

Request: `{ "package": "ms", "version": "2.1.3" }`. `package` must be a valid npm name (optionally scoped), `version` letters, digits and `.+-` only; other fields are rejected (`422`). Always `200` with a `status`; failures are reported as `unavailable`, never as a guess.

**Canonical manifest (schema 1):** `{"integrity": <npm dist.integrity>, "name", "schema_version": 1, "version"}` serialised as UTF-8 JSON with sorted keys and no whitespace; `manifest_hash = keccak256(bytes)`. Reference implementations: `backend/app/blockchain/manifest.py` and `contracts/scripts/manifest.ts`, pinned to shared test vectors.

`release_id` is `keccak256(utf8("<name>@<version>"))` as 0x-prefixed hex — the same id used by `ReleaseRegistry.anchor` (see `contracts/README.md`).

```jsonc
{
  "package": "ms", "version": "2.1.3",
  "release_id": "0x…",
  "status": "match",               // match | mismatch | no_record | unavailable
  "manifest_hash": "0x…",          // hash recomputed now; null if it could not be computed
  "anchored_hash": "0x…",          // hash stored on-chain; null if no record
  "anchored_at": "2026-10-12T10:00:00Z",   // null if no record
  "manifest": { "schema_version": 1, "name": "ms", "version": "2.1.3", "integrity": "sha512-…" },  // canonical fields, or null
  "registry": { "network": "hardhat-local", "chain_id": 31337, "address": "0x…" },             // null if not configured
  "detail": null                   // reason when status = unavailable (e.g. chain unreachable, registry not deployed)
}
```

Interpretation rules (UI and API must both follow):

- `match` — metadata equals the approved record. **Not** a safety verdict.
- `mismatch` — metadata differs from the approved record; investigate.
- `no_record` — evidence gap, **not** a risk signal. Must not be reported as a mismatch.
- `unavailable` — chain or metadata unreachable; say so, don't guess.

## Pull request trust gate — `POST /api/gate`

Compares a pull request's lockfile with its base and examines only the releases the head adds or updates. Runs synchronously; the body cap is twice the scan cap. The same logic is available as a CLI (`python -m app.gate`, exit `1` on fail) and as the repository's GitHub Action (`action.yml`).

```jsonc
// request
{
  "base_lockfile": { "lockfileVersion": 3, "packages": { … } },   // optional: omit when the PR adds the lockfile
  "head_lockfile": { "lockfileVersion": 3, "packages": { … } },
  "fail_on": "high",               // high | medium | low | never — lowest finding severity that fails
  "label": "frontend/package-lock.json"   // optional, shown in the report
}

// response
{
  "verdict": "fail",               // pass | warn | fail
  "fail_on": "high",
  "label": "frontend/package-lock.json",
  "summary": { "added": 2, "updated": 1, "removed": 1, "failed": 1, "warned": 1 },
  "releases": [
    {
      "package": "express",
      "version": "4.18.3",
      "change": "updated",          // added | updated
      "from_versions": ["4.18.2"],
      "direct": true,
      "verdict": "fail",
      "checks": [
        { "status": "pass", "label": "No known vulnerabilities" },        // pass | warn | fail | info
        { "status": "fail", "label": "New postinstall script introduced" }
      ],
      "findings": [ /* Finding objects, as in /findings */ ],
      "anchor": null,               // VerificationResult when a release registry is reachable
      "blast_radius": ["shop-app", "express"]
    }
  ],
  "removed_packages": ["left-pad@1.3.0"],
  "warnings": ["…"],               // report-level gaps, e.g. registry or OSV unavailable
  "notes": ["…"],                  // lockfile resolution notes about changed releases
  "markdown": "<!-- provenyx-trust-gate:frontend/package-lock.json -->
## ❌ Dependency Trust Check: FAILED …"
}
```

Verdict rules: a release fails when a finding's severity is at or above `fail_on`, or its anchor is a `mismatch`; any other `low`+ finding, or a release npm has no record of, warns. `info` findings and `no_record` anchors never warn. The overall verdict is the worst release verdict.

## Compatibility rules

- Additive response fields are non-breaking; clients must ignore unknown fields.
- Removing/renaming fields or tightening validation requires bumping the API version and updating the extension.
