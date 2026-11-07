const assert = require('node:assert');
const vscode = require('vscode');

exports.run = async function run() {
  const extension = vscode.extensions.getExtension('OmkarPramodBandikatte.provenyx-security');
  assert.ok(extension, 'packaged Provenyx extension not loaded');
  assert.strictEqual(extension.packageJSON.version, '0.1.6');
  assert.strictEqual(
    extension.packageJSON.contributes.configuration.properties['provenyx.apiBaseUrl'].default,
    'https://devknights.onrender.com',
  );
  assert.deepStrictEqual(extension.packageJSON.contributors, [
    'Aaditya Shivshankar Jaiswar',
    'Omkar Bandikatte',
    'RONAK PARMAR - 60009230091',
    'VIVEK CHAUHAN - 60009230029',
  ]);

  await extension.activate();
  assert.strictEqual(
    vscode.workspace.getConfiguration('provenyx').get('apiBaseUrl'),
    'https://devknights.onrender.com',
    'packaged activation must migrate the legacy localhost API setting',
  );

  const commands = await vscode.commands.getCommands(true);
  for (const id of [
    'provenyx.scanDependencies',
    'provenyx.showFindings',
    'provenyx.openDashboard',
    'provenyx.checkBackend',
    'provenyx.signIn',
  ]) {
    assert.ok(commands.includes(id), `missing command ${id}`);
  }

  const health = await fetch('https://devknights.onrender.com/health', {
    signal: AbortSignal.timeout(20_000),
  });
  assert.strictEqual(health.status, 200);
  assert.deepStrictEqual(await health.json(), {
    status: 'ok',
    service: 'provenyx-api',
    version: '0.1.0',
  });

  console.log('[provenyx package smoke] OK: 0.1.6 activation, migration, metadata, commands, Render health');
};
