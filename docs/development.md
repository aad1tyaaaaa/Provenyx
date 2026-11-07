# Development guide

## Prerequisites

| Tool | Version used to verify the project |
| --- | --- |
| Node.js | 24.18 (Hardhat 3 needs ≥ 22.10; extension/frontend need ≥ 20) |
| npm | 12.0 |
| Python | 3.13 (3.11+ supported) |
| VS Code | 1.141 (extension targets ≥ 1.90) |
| Git | any recent |

The first Hardhat compile downloads the solc 0.8.28 compiler; the first extension smoke test downloads a VS Code build. Both need internet access once.

> **npm 12 note:** npm 12 blocks dependency install scripts by default and prints `install-scripts … blocked` warnings for `esbuild` and `@vscode/vsce-sign`. Both still work for our use (esbuild ships its binary as a platform package). Do not blanket-approve install scripts.

## 1. Install dependencies

From the repository root:

```bash
npm run install:all          # extension, frontend, contracts
```

Backend (separate Python virtualenv):

```bash
cd backend
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# Windows (Git Bash):    source .venv/Scripts/activate
# macOS / Linux:         source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # optional; PowerShell: Copy-Item .env.example .env
```

Optional Gemini reranking is disabled by default. To enable it, set `PROVENYX_AI_RANKING_ENABLED=true` and `PROVENYX_GEMINI_API_KEY` in `backend/.env`. Never commit the key. Only generated finding summaries are sent to Gemini; lockfiles, dependency graphs, and raw evidence remain local. Invalid responses and provider failures fall back to deterministic ranking.

### GitHub project access

Create a GitHub OAuth App under **Settings → Developer settings → OAuth Apps**:

- Homepage URL: `http://localhost:3000`
- Authorization callback URL: `http://localhost:8000/api/github/auth/callback`

Then set `PROVENYX_GITHUB_CLIENT_ID`, `PROVENYX_GITHUB_CLIENT_SECRET`, and `PROVENYX_GITHUB_SESSION_SECRET` in `backend/.env`. Generate the session secret with `openssl rand -hex 32`. Restart the backend, open <http://localhost:3000/github>, and connect GitHub.

The OAuth token is AES-GCM encrypted in an HTTP-only cookie and expires after eight hours. Provenyx uses it only for read requests: listing accessible repositories, discovering `package-lock.json` and `composer.lock` files, and fetching the selected lockfile (plus, for Composer, the adjacent `composer.json` when present). GitHub's classic `repo` OAuth scope is broader than read-only because OAuth Apps do not offer a private-repository read-only scope; use a dedicated OAuth App and review that tradeoff before production deployment.

## 2. Start the FastAPI backend

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

Tests: `python -m pytest`

## 3. Start the Next.js frontend

```bash
npm run dev:frontend         # http://localhost:3000
```

Optional: `cp frontend/.env.example frontend/.env.local` to point at another API URL.

## 4. Launch the extension (Extension Development Host)

1. Open the **repository root** in VS Code.
2. Press **F5** (or Run and Debug → *Run Provenyx Extension*). This builds the extension and opens a new window on `demo/sample-project`.
3. In the new window: the shield icon in the Activity Bar opens the Provenyx sidebar; `Ctrl+Shift+P` → type “Provenyx”.

For rebuild-on-save run `npm --prefix extension run watch` and reload the host window (`Ctrl+R`).

Automated check (downloads and launches a real VS Code instance, then verifies activation, commands, and the view):

```bash
npm --prefix extension run test:smoke
# also run one real scan against a running backend:
PROVENYX_SMOKE_REAL_API=http://127.0.0.1:8000 npm --prefix extension run test:smoke
```

The smoke test confirms the consent dialog automatically and uses a throwaway in-test API (synthetic data) to check the scan → findings flow and the offline-backend case.

## 5. Check backend connectivity

```bash
curl http://localhost:8000/health
```

PowerShell: `Invoke-RestMethod http://localhost:8000/health`

In VS Code: **Provenyx: Check Backend Connection**, or look at *Backend* in the sidebar. On the dashboard, the status card shows the same.

## 6. Scan the sample lockfile

In the Extension Development Host (opened on `demo/sample-project`): **Provenyx: Scan Dependencies** → confirm **Send to Provenyx API**. Expect: “Lockfile received. Dependency analysis has started.” and *Last scan: Accepted* in the sidebar.

From the dashboard: open <http://localhost:3000/scan/new>, choose `demo/sample-project/package-lock.json`, tick the consent box, and click **Start scan**.

From GitHub: open <http://localhost:3000/github>, connect an account, choose **Find lockfiles** on a repository, and analyze one of the discovered lockfiles. Repository code is never cloned or executed.

Without VS Code (Git Bash / macOS / Linux):

```bash
curl -X POST http://localhost:8000/api/scans -H "Content-Type: application/json" \
  -d "{\"lockfile\": $(cat demo/sample-project/package-lock.json)}"
```

Use the returned `scan_id` to inspect the completed snapshot and graph:

```bash
curl http://localhost:8000/api/scans/<scan_id>
curl http://localhost:8000/api/scans/<scan_id>/graph
```

PowerShell:

```powershell
$lock = Get-Content demo/sample-project/package-lock.json -Raw | ConvertFrom-Json
Invoke-RestMethod -Method Post http://localhost:8000/api/scans -ContentType application/json `
  -Body (@{ lockfile = $lock } | ConvertTo-Json -Depth 50)
```

## 7. Package the extension as a VSIX

```bash
npm run package:extension    # → extension/provenyx-security-<version>.vsix
code --install-extension extension/provenyx-security-<version>.vsix
# Uninstall the obsolete local build first if present: code --uninstall-extension provenyx.provenyx-security
```

## Contracts (local chain)

```bash
npm run compile:contracts
npm run test:contracts
npm --prefix contracts run node          # terminal 3: local chain on :8545 (keep running)
npm --prefix contracts run deploy:local  # writes contracts/deployments/localhost.json for the backend
npm --prefix contracts run anchor:local  # anchors hashes from contracts/anchors.json
```

### Release verification end to end

```bash
npm --prefix contracts run node                              # terminal 3, keep running
npm --prefix contracts run deploy:local
cp demo/anchors.demo.json contracts/anchors.json             # or: cd backend && .venv/Scripts/python -m app.blockchain.manifest ms@2.1.3 > ../contracts/anchors.json
npm --prefix contracts run anchor:local
curl -X POST localhost:8000/api/releases/verify -H "Content-Type: application/json" -d '{"package":"ms","version":"2.1.3"}'   # match
curl -X POST localhost:8000/api/releases/verify -H "Content-Type: application/json" -d '{"package":"ms","version":"2.1.2"}'   # mismatch (deliberately altered demo record)
curl -X POST localhost:8000/api/releases/verify -H "Content-Type: application/json" -d '{"package":"ms","version":"2.0.0"}'   # no_record
```

Stop the node and the same call returns `unavailable`. In the dashboard, open a scan, select a package, and click **Verify release**. The backend reads the chain only; it holds no keys.

See `contracts/README.md` for the release id format and how verification results map to API statuses.

## Ports

| Service | Port |
| --- | --- |
| FastAPI | 8000 |
| Next.js | 3000 |
| Hardhat node | 8545 |
