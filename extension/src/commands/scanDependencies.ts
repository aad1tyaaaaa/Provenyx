import * as vscode from 'vscode';
import type { FindingsTreeProvider } from '../providers/findingsTreeProvider';
import type { Finding } from '../types';
import { ApiError, apiBaseUrl, checkHealth, getFindings, getScan, submitScan } from '../services/apiClient';
import { requireGitHubSession } from '../services/githubAuth';
import { openDashboard, scanPath } from '../utils/dashboard';
import { showApiError } from '../utils/errors';
import { findLockfiles, projectName, readLockfile } from '../utils/workspace';

const CLIENT = 'vscode-extension/0.1.5';
const POLL_INTERVAL_MS = 2_000;
const POLL_LIMIT_MS = 120_000;

/**
 * Reads a supported dependency lockfile and, after explicit confirmation, sends it to the Provenyx API.
 * Never installs or executes anything from the scanned project.
 */
export async function scanDependencies(tree: FindingsTreeProvider, target?: vscode.Uri): Promise<void> {
  if (!vscode.workspace.workspaceFolders?.length) {
    void vscode.window.showWarningMessage('Provenyx: Open a project folder to scan its dependencies.');
    return;
  }

  const session = await requireGitHubSession();
  if (!session) return;

  const uri = target ?? (await pickLockfile());
  if (!uri) return;
  const relPath = vscode.workspace.asRelativePath(uri);

  let lockfile;
  try {
    lockfile = await readLockfile(uri);
  } catch (err) {
    void vscode.window.showErrorMessage(`Provenyx: ${(err as Error).message}`);
    return;
  }

  const base = apiBaseUrl();
  const send = 'Send to Provenyx API';
  const choice = await vscode.window.showInformationMessage(
    `Provenyx found ${relPath}.`,
    {
      modal: true,
      detail:
        `Send dependency data from ${relPath} to ${base}?\n\n` +
        'No source files are uploaded and no packages are installed or executed.',
    },
    send,
  );
  if (choice !== send) return;

  const scanId = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'Provenyx: Submitting lockfile…' },
    async () => {
      try {
        await checkHealth();
        tree.setBackend('connected');
      } catch (err) {
        tree.setBackend('unreachable');
        fail(tree, err);
        return undefined;
      }
      try {
        const res = await submitScan(
          { lockfile, project_name: lockfile.name ?? projectName(), client: CLIENT },
          session.accessToken,
        );
        tree.setLastScan({ kind: 'in-progress', scanId: res.scan_id, status: 'accepted', message: res.message, at: new Date() });
        void vscode.window.showInformationMessage(`Provenyx: ${res.message}`, 'Open in Dashboard').then((pick) => {
          if (pick) void openDashboard(scanPath(res.scan_id));
        });
        return res.scan_id;
      } catch (err) {
        fail(tree, err);
        return undefined;
      }
    },
  );

  if (scanId) await followScan(tree, scanId, session.accessToken);
}

/**
 * Polls the scan until it completes or fails, then loads findings.
 * Stops quietly if the API does not provide scan status yet, or if a newer scan replaced this one.
 */
async function followScan(tree: FindingsTreeProvider, scanId: string, accessToken: string): Promise<void> {
  const isCurrent = () => {
    const s = tree.lastScan;
    return s.kind === 'in-progress' && s.scanId === scanId;
  };
  const deadline = Date.now() + POLL_LIMIT_MS;

  while (Date.now() < deadline && isCurrent()) {
    let scan;
    try {
      scan = await getScan(scanId, accessToken);
    } catch {
      return; // transient error: keep the "accepted" state; the dashboard can still show the scan
    }
    if (!scan || !isCurrent()) return;

    if (scan.status === 'failed') {
      fail(tree, new ApiError(scan.error ?? 'The scan failed.'), scanId);
      return;
    }
    if (scan.status === 'complete') {
      let findings: Finding[] | undefined;
      try {
        findings = await getFindings(scanId, accessToken);
      } catch {
        findings = undefined;
      }
      if (!isCurrent()) return;
      tree.setLastScan({ kind: 'complete', scan, findings, at: new Date() });
      if (findings?.length) {
        void vscode.window
          .showInformationMessage(`Provenyx: ${findings.length} finding(s) to review.`, 'Show Findings')
          .then((pick) => pick && vscode.commands.executeCommand('provenyx.showFindings'));
      }
      return;
    }
    tree.setLastScan({ kind: 'in-progress', scanId, status: scan.status, message: 'Analysis in progress.', at: new Date() });
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }
}

async function pickLockfile(): Promise<vscode.Uri | undefined> {
  const found = await findLockfiles();
  if (found.length === 0) {
    void vscode.window.showWarningMessage(
      'Provenyx: No supported dependency file found. Supported ecosystems: JavaScript, PHP, Python, Go, Rust, JVM, .NET, Ruby, Dart, and Swift.',
    );
    return undefined;
  }
  if (found.length === 1) return found[0];
  const picked = await vscode.window.showQuickPick(
    found.map((uri) => ({ label: vscode.workspace.asRelativePath(uri), uri })),
    { placeHolder: 'Select a dependency lockfile to scan' },
  );
  return picked?.uri;
}

function fail(tree: FindingsTreeProvider, err: unknown, scanId?: string): void {
  const message = err instanceof ApiError ? err.message : 'Unexpected error while contacting the Provenyx API.';
  tree.setLastScan({ kind: 'failed', scanId, message, at: new Date() });
  void showApiError(err);
}
