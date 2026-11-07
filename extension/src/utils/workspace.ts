import * as vscode from 'vscode';
import { parse as parseYaml } from 'yaml';
import {
  parseCargoLock,
  parseBunLock,
  parseGemfileLock,
  parseGoSum,
  parseGradleLock,
  parseMavenPom,
  parseNugetLock,
  parsePipfileLock,
  parsePubspecLock,
  parseRequirements,
  parseSwiftResolved,
  parseYarnLock,
} from '../adapters/ecosystems';
import type { Lockfile } from '../types';

/** Same cap the backend enforces by default (PROVENYX_MAX_SCAN_BYTES). */
export const MAX_LOCKFILE_BYTES = 10 * 1024 * 1024;

export function projectName(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.name;
}

export function isProjectLockfile(uri: vscode.Uri): boolean {
  return /\/(?:package-lock\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lock|composer\.lock|Pipfile\.lock|requirements\.txt|go\.sum|Cargo\.lock|pom\.xml|gradle\.lockfile|packages\.lock\.json|Gemfile\.lock|pubspec\.lock|Package\.resolved)$/.test(uri.path)
    && !uri.path.includes('/node_modules/')
    && !uri.path.includes('/vendor/')
    && !uri.path.includes('/.venv/')
    && !uri.path.includes('/venv/');
}

/** Finds supported dependency lockfiles in the workspace or a specific folder, ignoring node_modules. */
export async function findLockfiles(folder?: vscode.WorkspaceFolder): Promise<vscode.Uri[]> {
  const pattern = '**/{package-lock.json,pnpm-lock.yaml,yarn.lock,bun.lock,composer.lock,Pipfile.lock,requirements.txt,go.sum,Cargo.lock,pom.xml,gradle.lockfile,packages.lock.json,Gemfile.lock,pubspec.lock,Package.resolved}';
  const include = folder ? new vscode.RelativePattern(folder, pattern) : pattern;
  const found = await vscode.workspace.findFiles(include, '**/{node_modules,vendor,.venv,venv}/**', 100);
  return found.sort((a, b) => a.path.length - b.path.length);
}

/** Reads and structurally validates a lockfile. Throws an Error with a user-facing message. */
export async function readLockfile(uri: vscode.Uri): Promise<Lockfile> {
  const { size } = await vscode.workspace.fs.stat(uri);
  if (size > MAX_LOCKFILE_BYTES) {
    throw new Error(`${uri.path.split('/').pop()} is ${(size / 1048576).toFixed(1)} MB; the limit is 10 MB.`);
  }
  const text = new TextDecoder('utf-8').decode(await vscode.workspace.fs.readFile(uri));
  let lockfile: Lockfile;
  if (uri.path.endsWith('/pnpm-lock.yaml')) lockfile = parsePnpmLockfile(text);
  else if (uri.path.endsWith('/yarn.lock')) lockfile = parseYarnLock(text, await readAdjacentText(uri, 'package.json'));
  else if (uri.path.endsWith('/bun.lock')) lockfile = parseBunLock(text);
  else if (uri.path.endsWith('/Pipfile.lock')) lockfile = parsePipfileLock(text);
  else if (uri.path.endsWith('/requirements.txt')) lockfile = parseRequirements(text);
  else if (uri.path.endsWith('/go.sum')) {
    lockfile = parseGoSum(text, await readAdjacentText(uri, 'go.mod'));
  }
  else if (uri.path.endsWith('/Cargo.lock')) lockfile = parseCargoLock(text);
  else if (uri.path.endsWith('/pom.xml')) lockfile = parseMavenPom(text);
  else if (uri.path.endsWith('/gradle.lockfile')) lockfile = parseGradleLock(text);
  else if (uri.path.endsWith('/packages.lock.json')) lockfile = parseNugetLock(text);
  else if (uri.path.endsWith('/Gemfile.lock')) lockfile = parseGemfileLock(text);
  else if (uri.path.endsWith('/pubspec.lock')) lockfile = parsePubspecLock(text);
  else if (uri.path.endsWith('/Package.resolved')) lockfile = parseSwiftResolved(text);
  else if (uri.path.endsWith('/composer.lock')) {
    const manifestUri = vscode.Uri.joinPath(uri, '..', 'composer.json');
    let manifestText: string | undefined;
    try {
      manifestText = new TextDecoder('utf-8').decode(await vscode.workspace.fs.readFile(manifestUri));
    } catch {
      // composer.json improves direct-dependency classification but is not required for a useful scan.
    }
    lockfile = parseComposerLockfile(text, manifestText);
  } else {
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      throw new Error('package-lock.json is not valid JSON.');
    }
    lockfile = validateLockfile(data);
  }
  lockfile.provenyxComponentPath = componentPath(uri);
  return lockfile;
}

async function readAdjacentText(uri: vscode.Uri, name: string): Promise<string | undefined> {
  try {
    return new TextDecoder('utf-8').decode(await vscode.workspace.fs.readFile(vscode.Uri.joinPath(uri, '..', name)));
  } catch {
    return undefined;
  }
}

function componentPath(uri: vscode.Uri): string {
  const folder = vscode.workspace.getWorkspaceFolder(uri);
  if (!folder) return '.';
  const parent = vscode.Uri.joinPath(uri, '..').path;
  const root = folder.uri.path.replace(/\/+$/, '');
  const relative = parent === root ? '' : parent.startsWith(`${root}/`) ? parent.slice(root.length + 1) : '';
  return relative && !relative.split('/').includes('..') ? relative : '.';
}

type UnknownRecord = Record<string, unknown>;

export function parsePnpmLockfile(text: string): Lockfile {
  let data: unknown;
  try {
    data = parseYaml(text);
  } catch {
    throw new Error('pnpm-lock.yaml is not valid YAML.');
  }
  if (!isRecord(data) || !isRecord(data.importers) || !isRecord(data.packages) || !isRecord(data.snapshots)) {
    throw new Error('pnpm-lock.yaml must contain importers, packages, and snapshots objects.');
  }

  const importer = isRecord(data.importers['.']) ? data.importers['.'] : Object.values(data.importers).find(isRecord);
  const root = isRecord(importer) ? importer : {};
  const packageMetadata = data.packages;
  const packages: Record<string, unknown> = {
    '': {
      dependencies: normalizeDependencyMap(root.dependencies),
      devDependencies: normalizeDependencyMap(root.devDependencies),
      optionalDependencies: normalizeDependencyMap(root.optionalDependencies),
    },
  };

  for (const [snapshotKey, rawSnapshot] of Object.entries(data.snapshots)) {
    const identity = parsePnpmPackageKey(snapshotKey);
    if (!identity || !isRecord(rawSnapshot)) continue;
    const metadataKey = `${identity.name}@${identity.version}`;
    const metadata = isRecord(packageMetadata[snapshotKey])
      ? packageMetadata[snapshotKey]
      : isRecord(packageMetadata[metadataKey]) ? packageMetadata[metadataKey] : {};
    const resolution = isRecord(metadata.resolution) ? metadata.resolution : {};
    const virtualPath = `node_modules/.pnpm/${encodeURIComponent(snapshotKey)}/node_modules/${identity.name}`;
    packages[virtualPath] = {
      name: identity.name,
      version: identity.version,
      integrity: typeof resolution.integrity === 'string' ? resolution.integrity : undefined,
      dependencies: normalizeDependencyMap(rawSnapshot.dependencies),
      optionalDependencies: normalizeDependencyMap(rawSnapshot.optionalDependencies),
      peerDependencies: normalizeDependencyMap(rawSnapshot.peerDependencies),
    };
  }

  return validateLockfile({ lockfileVersion: 3, packages, provenyxSource: 'pnpm-lock.yaml' });
}

export function parseComposerLockfile(text: string, manifestText?: string): Lockfile {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error('composer.lock is not valid JSON.');
  }
  if (!isRecord(data) || !Array.isArray(data.packages) || !Array.isArray(data['packages-dev'])) {
    throw new Error('composer.lock must contain "packages" and "packages-dev" arrays.');
  }

  const warnings: string[] = [];
  let manifest: UnknownRecord = {};
  if (manifestText === undefined) {
    warnings.push('composer.json was not found; direct dependencies are unknown');
  } else {
    try {
      const parsed = JSON.parse(manifestText) as unknown;
      if (isRecord(parsed)) manifest = parsed;
      else warnings.push('composer.json does not contain a JSON object; direct dependencies are unknown');
    } catch {
      warnings.push('composer.json is invalid; direct dependencies are unknown');
    }
  }

  const rawPackages = [...data.packages, ...data['packages-dev']];
  const versions = new Map<string, string>();
  for (const rawPackage of rawPackages) {
    if (!isRecord(rawPackage) || typeof rawPackage.name !== 'string' || typeof rawPackage.version !== 'string') continue;
    versions.set(rawPackage.name, normalizeComposerVersion(rawPackage.version));
  }

  const packages: Record<string, unknown> = {
    '': {
      name: typeof manifest.name === 'string' ? manifest.name : undefined,
      dependencies: composerDependencyMap(manifest.require, versions),
      devDependencies: composerDependencyMap(manifest['require-dev'], versions),
    },
  };
  for (const rawPackage of rawPackages) {
    if (!isRecord(rawPackage) || typeof rawPackage.name !== 'string' || typeof rawPackage.version !== 'string') {
      warnings.push('Skipped a Composer package whose name or exact version is unavailable');
      continue;
    }
    const dist = isRecord(rawPackage.dist) ? rawPackage.dist : {};
    const source = isRecord(rawPackage.source) ? rawPackage.source : {};
    packages[`vendor/${rawPackage.name}`] = {
      name: rawPackage.name,
      version: normalizeComposerVersion(rawPackage.version),
      resolved: typeof dist.url === 'string' ? dist.url : typeof source.url === 'string' ? source.url : undefined,
      integrity: typeof dist.shasum === 'string' && dist.shasum ? dist.shasum : undefined,
      dependencies: composerDependencyMap(rawPackage.require, versions),
    };
  }

  return validateLockfile({
    name: typeof manifest.name === 'string' ? manifest.name : undefined,
    lockfileVersion: 3,
    packages,
    provenyxSource: 'composer.lock',
    provenyxWarnings: warnings,
  });
}

function composerDependencyMap(value: unknown, versions: Map<string, string>): Record<string, string> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.keys(value).flatMap((name) => {
      const version = versions.get(name);
      return version ? [[name, version]] : [];
    }),
  );
}

function normalizeComposerVersion(version: string): string {
  return /^v\d/.test(version) ? version.slice(1) : version;
}

function normalizeDependencyMap(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).flatMap(([name, rawValue]) => {
      const value = typeof rawValue === 'string'
        ? rawValue
        : isRecord(rawValue) && typeof rawValue.version === 'string' ? rawValue.version : undefined;
      const version = value && exactPnpmVersion(value);
      return version ? [[name, version]] : [];
    }),
  );
}

function parsePnpmPackageKey(key: string): { name: string; version: string } | undefined {
  const peerIndex = key.indexOf('(');
  const base = peerIndex >= 0 ? key.slice(0, peerIndex) : key;
  const separator = base.startsWith('@') ? base.indexOf('@', base.indexOf('/') + 1) : base.lastIndexOf('@');
  if (separator <= 0) return undefined;
  const name = base.slice(0, separator);
  const version = exactPnpmVersion(base.slice(separator + 1));
  return version ? { name, version } : undefined;
}

function exactPnpmVersion(value: string): string | undefined {
  if (/^(?:link|workspace|file):/.test(value)) return undefined;
  const peerIndex = value.indexOf('(');
  const withoutPeers = peerIndex >= 0 ? value.slice(0, peerIndex) : value;
  const aliasSeparator = withoutPeers.lastIndexOf('@');
  return aliasSeparator > 0 ? withoutPeers.slice(aliasSeparator + 1) : withoutPeers || undefined;
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function validateLockfile(data: unknown): Lockfile {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new Error('package-lock.json must contain a JSON object.');
  }
  const lock = data as Lockfile;
  if (![1, 2, 3].includes(lock.lockfileVersion)) {
    throw new Error(`Unsupported lockfileVersion: ${String(lock.lockfileVersion)}. Expected 1, 2 or 3.`);
  }
  const section = lock.lockfileVersion === 1 ? 'dependencies' : 'packages';
  const value = lock[section];
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`lockfileVersion ${lock.lockfileVersion} requires a "${section}" object.`);
  }
  return lock;
}
