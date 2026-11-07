import * as vscode from 'vscode';
import { isProjectLockfile } from '../utils/workspace';

const DEBOUNCE_MS = 1500;

/** Starts the consent-gated scan flow after a supported dependency lockfile changes. */
export function createLockfileWatcher(onChange: (uri: vscode.Uri) => void): vscode.Disposable {
  const watcher = vscode.workspace.createFileSystemWatcher('**/{package-lock.json,pnpm-lock.yaml,composer.lock}');
  const pending = new Map<string, NodeJS.Timeout>();

  const scan = (uri: vscode.Uri) => {
    pending.delete(uri.toString());
    if (!vscode.workspace.getConfiguration('provenyx').get<boolean>('autoScanEnabled', true)) return;
    onChange(uri);
  };

  // Package managers often write the lockfile more than once per install; collapse bursts into one prompt.
  const handler = (uri: vscode.Uri) => {
    if (!isProjectLockfile(uri)) return;
    const key = uri.toString();
    clearTimeout(pending.get(key));
    pending.set(key, setTimeout(() => scan(uri), DEBOUNCE_MS));
  };

  return vscode.Disposable.from(
    watcher,
    watcher.onDidChange(handler),
    watcher.onDidCreate(handler),
    { dispose: () => pending.forEach(clearTimeout) },
  );
}
