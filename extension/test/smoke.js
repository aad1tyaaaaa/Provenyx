// Extension Development Host smoke test: `npm run test:smoke` (downloads a VS Code build on first run).
// Runs inside VS Code against a throwaway in-test HTTP server that implements docs/api-contract.md.
// All data served below is SYNTHETIC test data and never leaves this test.
const assert = require('node:assert');
const fs = require('node:fs');
const http = require('node:http');
const vscode = require('vscode');

const SCAN_ID = '00000000-0000-4000-8000-000000000001';
const SYNTHETIC_FINDING = {
  id: 'f-1',
  rule_id: 'synthetic-test-rule',
  package: 'synthetic-pkg',
  version: '0.0.1',
  severity: 'medium',
  confidence: 'high',
  priority_score: 60,
  title: 'SYNTHETIC test finding',
  explanation: 'Test fixture only.',
  evidence: [{ source: 'lockfile', field: 'test', value: 'test', observed_at: '2026-10-12T00:00:00Z' }],
  paths: [['root', 'synthetic-pkg@0.0.1']],
};

/** mode "phase1": only health + POST /api/scans exist (older backend). mode "full": scan status + findings like the current backend. */
function startFakeApi(mode) {
  const server = http.createServer((req, res) => {
    const send = (status, body) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      if (req.method === 'GET' && req.url === '/health') return send(200, { status: 'ok', service: 'provenyx-api', version: '0.1.0' });
      if (req.method === 'POST' && req.url === '/api/github/auth/extension') {
        assert.strictEqual(req.headers.authorization, 'Bearer synthetic-github-token');
        server.handoffs = (server.handoffs ?? 0) + 1;
        return send(200, { url: 'https://dashboard.example/session' });
      }
      if (req.method === 'POST' && req.url === '/api/scans') {
        assert.strictEqual(req.headers.authorization, 'Bearer synthetic-github-token');
        const parsed = JSON.parse(body);
        assert.strictEqual(parsed.lockfile.lockfileVersion, 3, 'extension must send the parsed lockfile');
        server.lastRequestBody = body;
        server.lastProjectName = parsed.project_name;
        server.lastLockfile = parsed.lockfile;
        server.posts = (server.posts ?? 0) + 1;
        return send(202, { scan_id: SCAN_ID, status: 'accepted', message: 'Lockfile received.' });
      }
      if (mode === 'full' && req.url === `/api/scans/${SCAN_ID}`) {
        assert.strictEqual(req.headers.authorization, 'Bearer synthetic-github-token');
        return send(200, {
          scan_id: SCAN_ID, status: 'complete', created_at: new Date().toISOString(), project_name: 'x', error: null,
          summary: { packages: 2, direct: 1, transitive: 1, findings: 1 }, warnings: [],
        });
      }
      if (mode === 'full' && req.url === `/api/scans/${SCAN_ID}/findings`) {
        assert.strictEqual(req.headers.authorization, 'Bearer synthetic-github-token');
        return send(200, [SYNTHETIC_FINDING]);
      }
      send(404, { detail: 'Not Found' });
    });
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

async function useApi(url) {
  await vscode.workspace.getConfiguration('provenyx').update('apiBaseUrl', url, vscode.ConfigurationTarget.Global);
}

const labels = (items) => items.map((i) => `${i.label}${i.description ? ` | ${i.description}` : ''}`);

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
  assert.ok(ext, 'extension not loaded');
  const { tree } = await ext.activate();
  assert.ok(ext.isActive, 'extension did not activate');
  assert.strictEqual(
    vscode.workspace.getConfiguration('provenyx').get('apiBaseUrl'),
    'https://devknights.onrender.com',
    'activation must migrate the legacy localhost API setting',
  );

  const commands = await vscode.commands.getCommands(true);
  for (const id of ['provenyx.scanDependencies', 'provenyx.showFindings', 'provenyx.openDashboard', 'provenyx.checkBackend', 'provenyx.signIn']) {
    assert.ok(commands.includes(id), `missing command ${id}`);
  }
  await vscode.commands.executeCommand('provenyx.showFindings'); // throws if the view is not registered

  const lockfiles = await vscode.workspace.findFiles('**/package-lock.json', '**/node_modules/**');
  assert.strictEqual(lockfiles.length, 1, 'sample lockfile not found in workspace');

  // Auto-confirm the consent dialog (and only that dialog) so the flow can run unattended.
  const originalInfo = vscode.window.showInformationMessage;
  let consentShown = 0;
  let consentChoice;
  vscode.window.showInformationMessage = (message, ...rest) => {
    const opts = rest[0];
    if (opts && typeof opts === 'object' && opts.modal) {
      consentShown++;
      assert.match(opts.detail, /No source files are uploaded/);
      return Promise.resolve(consentChoice);
    }
    return Promise.resolve(undefined);
  };

  try {
    // 1. Dismissing consent must not contact the API.
    let api = await startFakeApi('phase1');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    await vscode.commands.executeCommand('provenyx.scanDependencies');
    assert.strictEqual(consentShown, 1, 'consent dialog must be shown before sending');
    assert.strictEqual(api.posts ?? 0, 0, 'dismissing consent must not upload the lockfile');
    api.close();

    // 2. Backend like today's Phase 1 API: scan accepted, no findings endpoint yet.
    consentChoice = 'Send to Provenyx API';
    api = await startFakeApi('phase1');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    await vscode.commands.executeCommand('provenyx.scanDependencies');
    assert.strictEqual(consentShown, 2, 'consent dialog must be shown before sending');
    assert.strictEqual(api.lastProjectName, 'provenyx-sample-project');
    assert.strictEqual(tree.backend, 'connected');
    assert.strictEqual(tree.lastScan.kind, 'in-progress');
    assert.ok(labels(tree.getChildren()).includes('No dependency findings yet'), labels(tree.getChildren()).join(', '));
    api.close();

    // 2. Full contract: scan completes and findings appear in the sidebar.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    await vscode.commands.executeCommand('provenyx.scanDependencies');
    assert.strictEqual(tree.lastScan.kind, 'complete');
    const group = tree.getChildren().find((i) => String(i.label).startsWith('Findings'));
    assert.ok(group, 'findings group missing');
    assert.strictEqual(group.label, 'Findings (1)');
    const [finding] = tree.getChildren(group);
    assert.strictEqual(finding.label, 'synthetic-pkg@0.0.1');
    assert.deepStrictEqual(finding.command.arguments, [`/scans/${SCAN_ID}?finding=f-1`]);
    api.close();

    // 4. pnpm v9: parse YAML locally and preserve exact root/transitive dependency versions.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const pnpmPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'pnpm-lock.yaml');
    await vscode.workspace.fs.writeFile(pnpmPath, Buffer.from(`lockfileVersion: '9.0'
importers:
  .:
    dependencies:
      parent:
        specifier: ^1.0.0
        version: 1.0.0
packages:
  parent@1.0.0:
    resolution: {integrity: sha512-parent}
  child@2.0.0:
    resolution: {integrity: sha512-child}
snapshots:
  parent@1.0.0:
    dependencies:
      child: 2.0.0
  child@2.0.0: {}
`));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', pnpmPath);
    } finally {
      await vscode.workspace.fs.delete(pnpmPath);
    }
    assert.strictEqual(api.lastLockfile.provenyxSource, 'pnpm-lock.yaml');
    assert.strictEqual(api.lastLockfile.packages[''].dependencies.parent, '1.0.0');
    const normalized = Object.values(api.lastLockfile.packages);
    assert.ok(normalized.some((entry) => entry.name === 'parent' && entry.version === '1.0.0' && entry.dependencies.child === '2.0.0'));
    assert.ok(normalized.some((entry) => entry.name === 'child' && entry.version === '2.0.0'));
    api.close();

    // 5. Composer: combine composer.lock with composer.json and preserve exact Packagist releases.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const composerPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'composer.lock');
    const composerManifestPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'composer.json');
    const phpPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'index.php');
    const phpSourceMarker = 'PHP_SOURCE_MUST_STAY_LOCAL_7f23c1';
    await vscode.workspace.fs.writeFile(phpPath, Buffer.from(`<?php\n\necho '${phpSourceMarker}';\n`));
    await vscode.workspace.fs.writeFile(composerManifestPath, Buffer.from(JSON.stringify({
      name: 'provenyx/php-demo',
      require: { php: '^8.2', 'symfony/console': '^7.1' },
    })));
    await vscode.workspace.fs.writeFile(composerPath, Buffer.from(JSON.stringify({
      packages: [
        { name: 'symfony/console', version: 'v7.1.3', require: { 'symfony/string': '^7.1' } },
        { name: 'symfony/string', version: 'v7.1.2' },
      ],
      'packages-dev': [],
    })));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', composerPath);
    } finally {
      await vscode.workspace.fs.delete(composerPath);
      await vscode.workspace.fs.delete(composerManifestPath);
      await vscode.workspace.fs.delete(phpPath);
    }
    assert.strictEqual(api.lastProjectName, 'provenyx/php-demo');
    assert.strictEqual(api.lastLockfile.provenyxSource, 'composer.lock');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { 'symfony/console': '7.1.3' });
    assert.strictEqual(api.lastLockfile.packages['vendor/symfony/console'].dependencies['symfony/string'], '7.1.2');
    assert.strictEqual(api.lastLockfile.packages['vendor/symfony/string'].version, '7.1.2');
    assert.ok(!api.lastRequestBody.includes(phpSourceMarker), 'PHP source must never be submitted');
    api.close();

    // 6. Python Pipenv: exact default/development releases are retained with hashes.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const pipfilePath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'Pipfile.lock');
    await vscode.workspace.fs.writeFile(pipfilePath, Buffer.from(JSON.stringify({
      _meta: { requires: { python_version: '3.12' } },
      default: { requests: { version: '==2.32.3', hashes: ['sha256:requests'] }, unpinned: { version: '*' } },
      develop: { pytest: { version: '==8.3.2' } },
    })));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', pipfilePath);
    } finally {
      await vscode.workspace.fs.delete(pipfilePath);
    }
    assert.strictEqual(api.lastLockfile.provenyxEcosystem, 'PyPI');
    assert.strictEqual(api.lastLockfile.provenyxPackageManager, 'pipenv');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { requests: '2.32.3' });
    assert.deepStrictEqual(api.lastLockfile.packages[''].devDependencies, { pytest: '8.3.2' });
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('unpinned')));
    api.close();

    // 7. Python requirements: only exact pins are scanned and conflicting duplicates are explicit.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const requirementsPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'requirements.txt');
    await vscode.workspace.fs.writeFile(requirementsPath, Buffer.from([
      'Django==5.1.1',
      'requests[security]==2.32.3; python_version >= "3.10"',
      'flask>=3.0',
      'django==4.2.0',
      'urllib3==2.*',
      '',
    ].join('\n')));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', requirementsPath);
    } finally {
      await vscode.workspace.fs.delete(requirementsPath);
    }
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { Django: '5.1.1', requests: '2.32.3' });
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('line 3')));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('conflicting requirement')));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('line 5')));
    api.close();

    // 8. Go modules: go.mod identifies direct modules and /go.mod checksum rows are ignored.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const goSumPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'go.sum');
    const goModPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'go.mod');
    await vscode.workspace.fs.writeFile(goModPath, Buffer.from('module example.com/demo\n\ngo 1.23\n\nrequire github.com/google/uuid v1.6.0\n'));
    await vscode.workspace.fs.writeFile(goSumPath, Buffer.from([
      'github.com/google/uuid v1.6.0 h1:module',
      'github.com/google/uuid v1.6.0/go.mod h1:manifest',
      'golang.org/x/text v0.18.0 h1:transitive',
      '',
    ].join('\n')));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', goSumPath);
    } finally {
      await vscode.workspace.fs.delete(goSumPath);
      await vscode.workspace.fs.delete(goModPath);
    }
    assert.strictEqual(api.lastLockfile.provenyxEcosystem, 'Go');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { 'github.com/google/uuid': 'v1.6.0' });
    assert.strictEqual(Object.values(api.lastLockfile.packages).filter((entry) => entry.version).length, 2);
    api.close();

    // 9. Rust Cargo: workspace roots are excluded and exact registry dependency edges are retained.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const cargoPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'Cargo.lock');
    await vscode.workspace.fs.writeFile(cargoPath, Buffer.from(`version = 3

[[package]]
name = "demo"
version = "0.1.0"
dependencies = [
 "serde 1.0.210",
]

[[package]]
name = "serde"
version = "1.0.210"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "serde-checksum"
dependencies = [
 "serde_derive 1.0.210",
]

[[package]]
name = "serde_derive"
version = "1.0.210"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "derive-checksum"
`));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', cargoPath);
    } finally {
      await vscode.workspace.fs.delete(cargoPath);
    }
    assert.strictEqual(api.lastLockfile.provenyxEcosystem, 'crates.io');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { serde: '1.0.210' });
    assert.ok(!Object.values(api.lastLockfile.packages).some((entry) => entry.name === 'demo'));
    assert.ok(Object.values(api.lastLockfile.packages).some(
      (entry) => entry.name === 'serde' && entry.dependencies.serde_derive === '1.0.210',
    ));
    api.close();

    // 10. Maven: exact direct dependencies are retained; properties and ranges are never guessed.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const pomPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'pom.xml');
    await vscode.workspace.fs.writeFile(pomPath, Buffer.from(`<?xml version="1.0"?>
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId><artifactId>demo</artifactId><version>1.0.0</version>
  <dependencies>
    <dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId><version>2.0.16</version></dependency>
    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId><version>5.11.0</version><scope>test</scope></dependency>
    <dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-databind</artifactId><version>{jackson.version}</version></dependency>
    <dependency><groupId>com.example</groupId><artifactId>dynamic</artifactId><version>LATEST</version></dependency>
  </dependencies>
</project>`.replace('\u001f', '$')));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', pomPath);
    } finally {
      await vscode.workspace.fs.delete(pomPath);
    }
    assert.strictEqual(api.lastProjectName, 'com.example:demo');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { 'org.slf4j:slf4j-api': '2.0.16' });
    assert.deepStrictEqual(api.lastLockfile.packages[''].devDependencies, { 'org.junit.jupiter:junit-jupiter': '5.11.0' });
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('jackson-databind')));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('dynamic')));
    api.close();

    // 11. Gradle: malformed lock coordinates are skipped and directness is not invented.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const gradlePath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'gradle.lockfile');
    await vscode.workspace.fs.writeFile(gradlePath, Buffer.from([
      '# This is a Gradle generated file',
      'com.google.guava:guava:33.3.0-jre=compileClasspath,runtimeClasspath',
      'malformed-coordinate=runtimeClasspath',
      'com.example:dynamic:1.+=runtimeClasspath',
      'empty=',
      '',
    ].join('\n')));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', gradlePath);
    } finally {
      await vscode.workspace.fs.delete(gradlePath);
    }
    assert.strictEqual(api.lastLockfile.provenyxPackageManager, 'gradle');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, {});
    assert.ok(Object.values(api.lastLockfile.packages).some(
      (entry) => entry.name === 'com.google.guava:guava' && entry.version === '33.3.0-jre',
    ));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('line 3')));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('line 4')));
    api.close();

    // 12. NuGet: target-framework duplicates collapse and direct/transitive edges remain exact.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const nugetPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'packages.lock.json');
    const nugetFramework = {
      'Newtonsoft.Json': { type: 'Direct', requested: '[13.0.3, )', resolved: '13.0.3', contentHash: 'json-hash', dependencies: { 'System.Memory': '4.5.5' } },
      'System.Memory': { type: 'Transitive', resolved: '4.5.5', contentHash: 'memory-hash' },
    };
    await vscode.workspace.fs.writeFile(nugetPath, Buffer.from(JSON.stringify({
      version: 1,
      dependencies: { 'net8.0': nugetFramework, 'net9.0': nugetFramework },
    })));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', nugetPath);
    } finally {
      await vscode.workspace.fs.delete(nugetPath);
    }
    assert.strictEqual(api.lastLockfile.provenyxEcosystem, 'NuGet');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { 'Newtonsoft.Json': '13.0.3' });
    const nugetPackages = Object.values(api.lastLockfile.packages).filter((entry) => entry.version);
    assert.strictEqual(nugetPackages.length, 2);
    assert.ok(nugetPackages.some(
      (entry) => entry.name === 'Newtonsoft.Json' && entry.dependencies['System.Memory'] === '4.5.5',
    ));
    api.close();

    // 13. Yarn: scoped descriptors, direct dependencies, integrity, and dependency edges are retained.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const yarnPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'yarn.lock');
    const yarnManifestPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'package.json');
    const originalManifest = await vscode.workspace.fs.readFile(yarnManifestPath);
    await vscode.workspace.fs.writeFile(yarnManifestPath, Buffer.from(JSON.stringify({
      name: 'yarn-demo', dependencies: { '@scope/parent': '^1.0.0' },
    })));
    await vscode.workspace.fs.writeFile(yarnPath, Buffer.from(`# yarn lockfile v1

"@scope/parent@^1.0.0":
  version "1.0.2"
  integrity sha512-parent
  dependencies:
    child "^2.0.0"
    ambiguous "^1.0.0"

child@^2.0.0:
  version "2.1.0"
  integrity sha512-child

ambiguous@^1.0.0:
  version "1.1.0"

ambiguous@^2.0.0:
  version "2.1.0"
`));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', yarnPath);
    } finally {
      await vscode.workspace.fs.delete(yarnPath);
      await vscode.workspace.fs.writeFile(yarnManifestPath, originalManifest);
    }
    assert.strictEqual(api.lastProjectName, 'yarn-demo');
    assert.strictEqual(api.lastLockfile.provenyxPackageManager, 'yarn');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { '@scope/parent': '1.0.2' });
    assert.ok(Object.values(api.lastLockfile.packages).some(
      (entry) => entry.name === '@scope/parent' && entry.dependencies.child === '2.1.0',
    ));
    assert.ok(Object.values(api.lastLockfile.packages).some(
      (entry) => entry.name === '@scope/parent' && !('ambiguous' in entry.dependencies),
    ));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('ambiguous')));
    api.close();

    // 14. Bun: JSONC comments/trailing commas and exact root/transitive releases are supported.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const bunPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'bun.lock');
    await vscode.workspace.fs.writeFile(bunPath, Buffer.from(`{
  // Bun text lockfile
  "lockfileVersion": 1,
  "workspaces": { "": { "name": "bun-demo", "dependencies": { "parent": "^1.0.0", "local": "workspace:*" } } },
  "packages": {
    "parent": ["parent@1.2.0", "", { "child": "^2.0.0" }, "sha512-parent"],
    "child": ["child@2.3.0", "", {}, "sha512-child"],
    "local": ["local@workspace:packages/local", "", {}],
  },
}`));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', bunPath);
    } finally {
      await vscode.workspace.fs.delete(bunPath);
    }
    assert.strictEqual(api.lastProjectName, 'bun-demo');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { parent: '1.2.0' });
    assert.ok(Object.values(api.lastLockfile.packages).some(
      (entry) => entry.name === 'parent' && entry.dependencies.child === '2.3.0',
    ));
    assert.ok(!Object.values(api.lastLockfile.packages).some((entry) => entry.name === 'local'));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('local')));
    api.close();

    // 15. Ruby Bundler: GEM specs and direct dependencies form an exact graph.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const gemPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'Gemfile.lock');
    await vscode.workspace.fs.writeFile(gemPath, Buffer.from(`GEM
  remote: https://rubygems.org/
  specs:
    rack (3.1.7)
      base64
    base64 (0.2.0)

PLATFORMS
  ruby

DEPENDENCIES
  rack (~> 3.1)
  missing (~> 1.0)

BUNDLED WITH
   2.5.18
`));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', gemPath);
    } finally {
      await vscode.workspace.fs.delete(gemPath);
    }
    assert.strictEqual(api.lastLockfile.provenyxEcosystem, 'RubyGems');
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { rack: '3.1.7' });
    assert.ok(Object.values(api.lastLockfile.packages).some(
      (entry) => entry.name === 'rack' && entry.dependencies.base64 === '0.2.0',
    ));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('missing')));
    api.close();

    // 16. Dart Pub: direct main/dev and transitive releases remain distinct.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const pubPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'pubspec.lock');
    await vscode.workspace.fs.writeFile(pubPath, Buffer.from(`packages:
  http:
    dependency: "direct main"
    description: {name: http, url: "https://pub.dev"}
    source: hosted
    version: "1.2.2"
  test:
    dependency: "direct dev"
    description: {name: test, url: "https://pub.dev"}
    source: hosted
    version: "1.25.8"
  collection:
    dependency: transitive
    description: {name: collection, url: "https://pub.dev"}
    source: hosted
    version: "1.19.0"
  dynamic:
    dependency: transitive
    description: {name: dynamic, url: "https://pub.dev"}
    source: hosted
    version: "1.*"
sdks: {dart: ">=3.5.0 <4.0.0"}
`));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', pubPath);
    } finally {
      await vscode.workspace.fs.delete(pubPath);
    }
    assert.deepStrictEqual(api.lastLockfile.packages[''].dependencies, { http: '1.2.2' });
    assert.deepStrictEqual(api.lastLockfile.packages[''].devDependencies, { test: '1.25.8' });
    assert.ok(Object.values(api.lastLockfile.packages).some((entry) => entry.name === 'collection'));
    assert.ok(!Object.values(api.lastLockfile.packages).some((entry) => entry.name === 'dynamic'));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('dynamic')));
    api.close();

    // 17. Swift: semantic-version pins are scanned while revision-only pins are explicitly skipped.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    const swiftPath = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'Package.resolved');
    await vscode.workspace.fs.writeFile(swiftPath, Buffer.from(JSON.stringify({
      version: 2,
      pins: [
        { identity: 'swift-nio', location: 'https://github.com/apple/swift-nio.git', state: { revision: 'abc123', version: '2.72.0' } },
        { identity: 'local-branch', location: 'https://example.test/local.git', state: { revision: 'def456', branch: 'main' } },
        { identity: 'invalid-version', location: 'https://example.test/invalid.git', state: { revision: 'ghi789', version: 'main' } },
      ],
    })));
    try {
      await vscode.commands.executeCommand('provenyx.scanDependencies', swiftPath);
    } finally {
      await vscode.workspace.fs.delete(swiftPath);
    }
    assert.strictEqual(api.lastLockfile.provenyxEcosystem, 'SwiftURL');
    assert.strictEqual(Object.values(api.lastLockfile.packages).filter((entry) => entry.version).length, 1);
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('local-branch')));
    assert.ok(api.lastLockfile.provenyxWarnings.some((warning) => warning.includes('invalid-version')));
    api.close();

    // 18. Backend offline: no crash, clear state.
    await useApi('http://127.0.0.1:9');
    const errors = [];
    const originalError = vscode.window.showErrorMessage;
    vscode.window.showErrorMessage = (message, ...items) => (errors.push({ message, items }), Promise.resolve(undefined));
    await vscode.commands.executeCommand('provenyx.checkBackend');
    await new Promise((r) => setTimeout(r, 100));
    vscode.window.showErrorMessage = originalError;
    assert.strictEqual(tree.backend, 'unreachable');
    assert.ok(errors.length === 1 && /npm run dev:backend/.test(errors[0].message), JSON.stringify(errors));
    assert.deepStrictEqual(errors[0].items, ['Retry', 'Copy Start Command', 'Open Settings']);
    const backendItem = tree.getChildren().find((i) => i.label === 'Backend');
    assert.match(backendItem.tooltip, /npm run dev:backend/);
    await vscode.commands.executeCommand('provenyx.scanDependencies');
    assert.strictEqual(tree.lastScan.kind, 'failed');
    assert.match(tree.lastScan.message, /Cannot reach the Provenyx API/);

    // 19. Lockfile watcher: a burst of writes starts ONE consent-gated scan, and declining sends nothing.
    api = await startFakeApi('full');
    await useApi(`http://127.0.0.1:${api.address().port}`);
    consentChoice = undefined;
    await vscode.workspace.getConfiguration('provenyx').update('autoScanEnabled', true, vscode.ConfigurationTarget.Global);
    const consentBeforeWatcher = consentShown;
    const lockPath = lockfiles[0].fsPath;
    const original = fs.readFileSync(lockPath, 'utf8');
    for (let i = 0; i < 3; i++) {
      fs.writeFileSync(lockPath, original);
      await new Promise((r) => setTimeout(r, 150));
    }
    await new Promise((r) => setTimeout(r, 3500)); // > 1.5 s debounce
    assert.strictEqual(consentShown, consentBeforeWatcher + 1, 'watcher must start one consent-gated scan');
    assert.strictEqual(api.posts ?? 0, 0, 'declining automatic scan consent must not send the lockfile');
    await vscode.workspace.getConfiguration('provenyx').update('autoScanEnabled', false, vscode.ConfigurationTarget.Global);
    fs.writeFileSync(lockPath, original);
    await new Promise((r) => setTimeout(r, 2000));
    assert.strictEqual(consentShown, consentBeforeWatcher + 1, 'watcher must stay silent when autoScanEnabled is off');
    await vscode.workspace.getConfiguration('provenyx').update('autoScanEnabled', undefined, vscode.ConfigurationTarget.Global);
    api.close();

    // 20. Optional: the real backend, if PROVENYX_SMOKE_REAL_API is set (e.g. http://127.0.0.1:8000).
    const real = process.env.PROVENYX_SMOKE_REAL_API;
    if (real) {
      await useApi(real);
      await vscode.commands.executeCommand('provenyx.scanDependencies');
      const s = tree.lastScan;
      assert.strictEqual(s.kind, 'complete', `real API: ${JSON.stringify(s)}`);
      assert.ok(Array.isArray(s.findings), 'real API: findings should load after completion');
      console.log(`[provenyx smoke] real API ${real}: scan ${s.scan.scan_id} complete, ${s.scan.summary?.packages} packages, ${s.findings.length} findings`);
    }
  } finally {
    vscode.window.showInformationMessage = originalInfo;
    await useApi(undefined);
  }

  console.log('[provenyx smoke] OK: activation, commands, consent, scan→findings, offline backend, debounced watcher');
};
