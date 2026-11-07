// Runs in a VS Code window whose workspace folder has no package-lock.json.
const assert = require('node:assert');
const vscode = require('vscode');

exports.run = async function run() {
  try {
    await runChecks();
  } catch (err) {
    console.error('[provenyx smoke] FAILED:', err && err.stack ? err.stack : err);
    throw err;
  }
};

async function runChecks() {
  const ext = vscode.extensions.all.find((e) => e.packageJSON.name === 'provenyx-security');
  const { tree } = await ext.activate();

  const warnings = [];
  const originalWarn = vscode.window.showWarningMessage;
  const originalInfo = vscode.window.showInformationMessage;
  let consentShown = false;
  vscode.window.showWarningMessage = (message) => (warnings.push(message), Promise.resolve(undefined));
  vscode.window.showInformationMessage = (message, opts) => ((consentShown ||= !!opts?.modal), Promise.resolve(undefined));
  try {
    await vscode.commands.executeCommand('provenyx.scanDependencies');
  } finally {
    vscode.window.showWarningMessage = originalWarn;
    vscode.window.showInformationMessage = originalInfo;
  }

  assert.strictEqual(warnings.length, 1, warnings.join(' | '));
  assert.match(warnings[0], /No supported dependency file found/);
  assert.strictEqual(consentShown, false, 'no consent dialog without a lockfile');
  assert.strictEqual(tree.lastScan.kind, 'none');
  console.log('[provenyx smoke] OK: no-lockfile workspace handled');
};
