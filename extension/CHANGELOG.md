# Changelog

## Unreleased

- Add PHP Composer support for `composer.lock`, using adjacent `composer.json` declarations to classify direct dependencies.
- Query OSV with the `Packagist` ecosystem and avoid npm metadata requests for Composer packages.

## Unreleased

- Ignore the legacy extension's `http://localhost:8000` default for `provenyx.apiBaseUrl`.
- Keep activating when the legacy extension already registered the Provenyx commands.
- Add an **Uninstall** button to the legacy extension warning.

## 0.1.5

- Migrate legacy localhost API settings to the deployed Render backend once.
- Warn when the obsolete `Provenyx.provenyx-security` extension is installed alongside this extension.
- Authenticate extension scan creation and reads with the signed-in GitHub session.

## 0.1.4

- Use the deployed Render backend for API requests by default.

## 0.1.3

- Request GitHub authentication in VS Code when the extension starts and require it before scanning.
- Carry the authenticated GitHub session into dashboard links through a short-lived, single-use server handoff, avoiding a second website login.
- Use the deployed Provenyx gateway and dashboard by default.
- Discover and scan every supported dependency lockfile across local workspace folders.
- Add local parsing and automatic change detection for pnpm v9 `pnpm-lock.yaml` files.
- Preserve pnpm direct and transitive dependency edges without uploading source files.

## 0.1.2

- Start the consent-gated scan flow when opening a project that already contains `package-lock.json`.
- Activate in workspaces without a lockfile so creating one later also triggers automatic scanning.
- Scan newly added workspace folders without requiring the manual Run command.

## 0.1.0 (preview)

- **Provenyx: Scan Dependencies** finds `package-lock.json` (v1–v3), validates it locally, and sends it to the Provenyx API only after you confirm.
- The sidebar shows the project, backend connection, last scan status, and findings with severity, confidence, explanation, and dependency path.
- After a scan, the extension follows its status and loads findings once the API provides them. Clicking a finding opens it in the web dashboard.
- **Provenyx: Open Dashboard** and **Provenyx: Check Backend Connection**.
- Settings: `provenyx.apiBaseUrl`, `provenyx.dashboardUrl`, `provenyx.autoScanEnabled` (on by default; still asks before sending).
- The dashboard verifies releases against blockchain-anchored manifests: match, mismatch, no record, or unavailable.
- No telemetry. Never installs or executes packages.
