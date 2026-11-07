# Architecture

```text
 VS Code                                   Browser
┌──────────────────────────┐             ┌──────────────────────────┐
│ Provenyx extension (TS)  │             │ Next.js dashboard :3000  │
│  • find dependency locks │             │  • /dashboard /scans     │
│  • validate JSON locally │             │  • /settings             │
│  • ask consent ──────────┼──POST───┐   │  • health status         │
│  • sidebar TreeView      │         │   └────────────┬─────────────┘
└──────────┬───────────────┘         │                │ GET /health (CORS)
           │ GET /health             ▼                ▼
           │                ┌─────────────────────────────────┐
           └───────────────►│ FastAPI backend :8000           │
                            │  api/      HTTP routes          │
                            │  models/   Pydantic contract    │
                            │  parsers/  lockfile + npm metadata│
                            │  graph/    graph + comparisons    │
                            │  detection/ explainable rules(P2+)
                            │  blockchain/ hash verification   │
                            └───────────────┬─────────────────┘
                                            │ (later phase, read-only)
                                            ▼
                            ┌─────────────────────────────────┐
                            │ ReleaseRegistry.sol (Hardhat)   │
                            │ releaseId → manifest hash       │
                            └─────────────────────────────────┘
```

## Components

| Path | Stack | Role |
| --- | --- | --- |
| `extension/` | TypeScript, VS Code API, esbuild | Primary product. Finds and validates npm, pnpm, and Composer lockfiles, obtains consent, submits to the API, shows state in a TreeView. |
| `backend/` | Python 3.11+, FastAPI, Pydantic | Validates submissions; future home of parsing, graph building, detection, and verification. |
| `frontend/` | Next.js App Router, Tailwind v4, shadcn/ui conventions, Lucide | Web dashboard for deeper investigation. |
| `contracts/` | Solidity, Hardhat 3, viem | Registry of approved release-manifest hashes. Local network only. |
| `demo/` | — | Sample project with a tiny lockfile for end-to-end checks. |

The monorepo does **not** use npm workspaces: each JS package has its own `node_modules` and lockfile, which keeps vsce packaging, Next.js, and Hardhat 3 (ESM) independent. The root `package.json` only holds convenience scripts. Python dependencies live in `backend/requirements.txt`.

## Trust boundaries and safety rules

- The extension reads `package-lock.json`, `pnpm-lock.yaml`, or `composer.lock` with `vscode.workspace.fs` and never runs a package manager, package code, or lifecycle scripts. Composer scans optionally read dependency declarations from adjacent `composer.json` to identify direct packages.
- Nothing leaves the machine without a modal confirmation for each scan, naming the destination URL. Only the lockfile is sent.
- The backend treats lockfile content as untrusted data: size-capped body (checked while streaming), strict schema, no echo of input in errors.
- Error messages shown to users never include lockfile contents.
- Registry requests retrieve public metadata only, use bounded concurrency/timeouts/retries, and report failures as unavailable rather than inventing values.
- Blockchain results are evidence, not verdicts: a match is not proof of safety; no record is not proof of compromise.
- Release verification is read-only (`eth_call` over JSON-RPC): the backend holds no keys and never writes to the chain. Anchoring is a separate, owner-only step in `contracts/`.

## Persistence

Phase 3 stores submitted scan snapshots and completed graphs in SQLite through SQLAlchemy. The backend always loads `backend/.env`; configure it there with `PROVENYX_DATABASE_URL`. Its default `sqlite:///./provenyx.db` is anchored to the backend directory, so every launch method uses `backend/provenyx.db`. A production move to PostgreSQL still requires migrations before deployment.
