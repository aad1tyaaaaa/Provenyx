const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { runTests } = require('@vscode/test-electron');

const extensionDevelopmentPath = path.resolve(__dirname, '..');

async function main() {
  process.env.PROVENYX_AUTH_PROVIDER = 'provenyx-test';
  const vscodeExecutablePath = process.env.VSCODE_EXECUTABLE_PATH;
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'provenyx-vscode-profile-'));
  fs.mkdirSync(path.join(userData, 'User'));
  fs.writeFileSync(
    path.join(userData, 'User/settings.json'),
    JSON.stringify({
      'provenyx.apiBaseUrl': 'http://localhost:8000',
      'provenyx.autoScanEnabled': false,
    }),
  );

  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'provenyx-empty-'));
  try {
    await runTests({
      extensionDevelopmentPath,
      extensionTestsPath: path.resolve(__dirname, 'smoke.js'),
      vscodeExecutablePath,
      launchArgs: [path.resolve(__dirname, '../../demo/sample-project'), '--disable-extensions', `--user-data-dir=${userData}`],
    });

    await runTests({
      extensionDevelopmentPath,
      extensionTestsPath: path.resolve(__dirname, 'smoke-empty.js'),
      vscodeExecutablePath,
      launchArgs: [empty, '--disable-extensions', `--user-data-dir=${userData}`],
    });
  } finally {
    fs.rmSync(empty, { recursive: true, force: true });
    fs.rmSync(userData, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
