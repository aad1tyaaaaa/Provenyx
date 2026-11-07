import { XMLParser } from 'fast-xml-parser';
import { parse as parseJsonc, type ParseError } from 'jsonc-parser/lib/esm/main.js';
import { parseSyml } from '@yarnpkg/parsers';
import { parse as parseYaml } from 'yaml';
import type { Lockfile } from '../types';

type UnknownRecord = Record<string, unknown>;

interface PackageInput {
  name: string;
  version: string;
  dependencies?: Record<string, string>;
  integrity?: string;
}

interface SnapshotInput {
  projectName?: string;
  ecosystem: string;
  packageManager: string;
  source: string;
  rootDependencies: Record<string, string>;
  rootDevDependencies?: Record<string, string>;
  packages: PackageInput[];
  warnings?: string[];
}

export function parsePipfileLock(text: string): Lockfile {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error('Pipfile.lock is not valid JSON.');
  }
  if (!isRecord(data) || !isRecord(data.default) || !isRecord(data.develop)) {
    throw new Error('Pipfile.lock must contain "default" and "develop" objects.');
  }
  const warnings: string[] = [];
  const prod = pythonSection(data.default, warnings);
  const dev = pythonSection(data.develop, warnings);
  return snapshot({
    ecosystem: 'PyPI',
    packageManager: 'pipenv',
    source: 'Pipfile.lock',
    rootDependencies: Object.fromEntries(prod.map(({ name, version }) => [name, version])),
    rootDevDependencies: Object.fromEntries(dev.map(({ name, version }) => [name, version])),
    packages: [...prod, ...dev],
    warnings: [...warnings, 'Pipfile.lock does not record transitive dependency edges; dependency paths are unavailable.'],
  });
}

export function parseRequirements(text: string): Lockfile {
  const warnings: string[] = [];
  const packages = new Map<string, PackageInput>();
  for (const [index, rawLine] of text.split(/\r?\n/).entries()) {
    const line = rawLine.replace(/\s+#.*$/, '').trim();
    if (!line || line.startsWith('#')) continue;
    const match = /^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==([^\s;]+)(?:\s*;.*)?$/.exec(line);
    if (!match) {
      warnings.push(`Skipped requirements.txt line ${index + 1}: an exact == version is required`);
      continue;
    }
    const [, name, version] = match;
    if (!isExactVersion(version)) {
      warnings.push(`Skipped requirements.txt line ${index + 1}: an exact == version is required`);
      continue;
    }
    const key = name.toLowerCase().replace(/[-_.]+/g, '-');
    const existing = packages.get(key);
    if (existing && existing.version !== version) {
      warnings.push(`Skipped conflicting requirement ${name}==${version}; ${existing.version} was already selected`);
      continue;
    }
    packages.set(key, { name, version });
  }
  return snapshot({
    ecosystem: 'PyPI',
    packageManager: 'pip',
    source: 'requirements.txt',
    rootDependencies: Object.fromEntries([...packages.values()].map(({ name, version }) => [name, version])),
    packages: [...packages.values()],
    warnings: [...warnings, 'requirements.txt does not record transitive dependency edges; every pinned entry is treated as direct.'],
  });
}

export function parseGoSum(text: string, goModText?: string): Lockfile {
  const packages = new Map<string, PackageInput>();
  for (const rawLine of text.split(/\r?\n/)) {
    const [name, version, checksum, extra] = rawLine.trim().split(/\s+/);
    if (!name || !version || !checksum || extra || version.endsWith('/go.mod')) continue;
    packages.set(`${name}@${version}`, { name, version, integrity: checksum });
  }
  const direct = goModText ? parseGoModRequires(goModText) : {};
  const installed = new Set([...packages.values()].map(({ name, version }) => `${name}@${version}`));
  const rootDependencies = Object.fromEntries(
    Object.entries(direct).filter(([name, version]) => installed.has(`${name}@${version}`)),
  );
  const warnings = [
    'go.sum records verified module versions but not a complete resolved dependency graph; dependency paths are unavailable.',
  ];
  if (!goModText) warnings.unshift('go.mod was not found; direct dependencies are unknown.');
  return snapshot({
    ecosystem: 'Go',
    packageManager: 'go-modules',
    source: 'go.sum',
    rootDependencies,
    packages: [...packages.values()],
    warnings,
  });
}

export function parseCargoLock(text: string): Lockfile {
  const records = text.split(/^\s*\[\[package\]\]\s*$/m).slice(1).map(parseCargoPackage).filter(isPackageInput);
  if (records.length === 0) throw new Error('Cargo.lock does not contain any valid [[package]] entries.');

  const external = records.filter((record) => record.source !== undefined);
  const externalByName = new Map<string, PackageInput[]>();
  for (const { source: _source, ...record } of external) {
    const matches = externalByName.get(record.name) ?? [];
    matches.push(record);
    externalByName.set(record.name, matches);
  }
  const resolve = (dependency: string): [string, string] | undefined => {
    const match = /^(.*?)(?:\s+([^\s]+))?(?:\s+\(.*\))?$/.exec(dependency);
    if (!match) return undefined;
    const [, name, requestedVersion] = match;
    const candidates = externalByName.get(name) ?? [];
    const target = requestedVersion ? candidates.find((candidate) => candidate.version === requestedVersion) : candidates[0];
    return target && (requestedVersion || candidates.length === 1) ? [target.name, target.version] : undefined;
  };
  const packageInputs = external.map(({ source: _source, rawDependencies, ...record }) => ({
    ...record,
    dependencies: Object.fromEntries((rawDependencies ?? []).flatMap((dependency) => {
      const resolved = resolve(dependency);
      return resolved ? [resolved] : [];
    })),
  }));
  const roots = records.filter((record) => record.source === undefined);
  const rootDependencies = Object.fromEntries(
    roots.flatMap((root) => (root.rawDependencies ?? []).flatMap((dependency) => {
      const resolved = resolve(dependency);
      return resolved ? [resolved] : [];
    })),
  );
  return snapshot({
    ecosystem: 'crates.io',
    packageManager: 'cargo',
    source: 'Cargo.lock',
    rootDependencies,
    packages: packageInputs,
    warnings: roots.length === 0 ? ['Cargo workspace roots are unavailable; direct dependencies are unknown.'] : [],
  });
}

export function parseMavenPom(text: string): Lockfile {
  let document: unknown;
  try {
    document = new XMLParser({ ignoreAttributes: false, parseTagValue: false, trimValues: true }).parse(text);
  } catch {
    throw new Error('pom.xml is not valid XML.');
  }
  if (!isRecord(document) || !isRecord(document.project)) throw new Error('pom.xml must contain a project element.');
  const project = document.project;
  const dependenciesNode = isRecord(project.dependencies) ? project.dependencies.dependency : undefined;
  const dependencies = (Array.isArray(dependenciesNode) ? dependenciesNode : dependenciesNode ? [dependenciesNode] : [])
    .filter(isRecord);
  const warnings = ['pom.xml contains direct declarations only; resolved transitive dependencies require a lockfile.'];
  const prod: PackageInput[] = [];
  const dev: PackageInput[] = [];
  for (const dependency of dependencies) {
    const group = stringValue(dependency.groupId);
    const artifact = stringValue(dependency.artifactId);
    const version = stringValue(dependency.version);
    if (!group || !artifact || !version || !isExactVersion(version) || /[${}\[\]()]/.test(version)
      || /^(?:LATEST|RELEASE)$/i.test(version)) {
      warnings.push(`Skipped Maven dependency ${group && artifact ? `${group}:${artifact}` : '(unknown)'}: an exact version is required`);
      continue;
    }
    const target = stringValue(dependency.scope) === 'test' ? dev : prod;
    target.push({ name: `${group}:${artifact}`, version });
  }
  const group = stringValue(project.groupId) ?? (isRecord(project.parent) ? stringValue(project.parent.groupId) : undefined);
  const artifact = stringValue(project.artifactId);
  return snapshot({
    projectName: group && artifact ? `${group}:${artifact}` : artifact,
    ecosystem: 'Maven',
    packageManager: 'maven',
    source: 'pom.xml',
    rootDependencies: Object.fromEntries(prod.map(({ name, version }) => [name, version])),
    rootDevDependencies: Object.fromEntries(dev.map(({ name, version }) => [name, version])),
    packages: [...prod, ...dev],
    warnings,
  });
}

export function parseGradleLock(text: string): Lockfile {
  const warnings: string[] = [];
  const packages: PackageInput[] = [];
  for (const [index, rawLine] of text.split(/\r?\n/).entries()) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || line.startsWith('empty=')) continue;
    const coordinate = line.split('=', 1)[0];
    const parts = coordinate.split(':');
    if (parts.length !== 3 || parts.some((part) => !part)) {
      warnings.push(`Skipped Gradle lock line ${index + 1}: expected group:artifact:version`);
      continue;
    }
    if (!isExactVersion(parts[2]) || parts[2].endsWith('+') || /^(?:latest\.|release$)/i.test(parts[2])) {
      warnings.push(`Skipped Gradle lock line ${index + 1}: an exact version is required`);
      continue;
    }
    packages.push({ name: `${parts[0]}:${parts[1]}`, version: parts[2] });
  }
  return snapshot({
    ecosystem: 'Maven',
    packageManager: 'gradle',
    source: 'gradle.lockfile',
    rootDependencies: {},
    packages,
    warnings: [...warnings, 'Gradle lock state does not identify direct dependencies or dependency edges.'],
  });
}

export function parseNugetLock(text: string): Lockfile {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error('packages.lock.json is not valid JSON.');
  }
  if (!isRecord(data) || !isRecord(data.dependencies)) {
    throw new Error('packages.lock.json must contain a dependencies object.');
  }
  const records = new Map<string, { pkg: PackageInput; direct: boolean }>();
  const warnings: string[] = [];
  for (const framework of Object.values(data.dependencies)) {
    if (!isRecord(framework)) continue;
    for (const [name, rawPackage] of Object.entries(framework)) {
      if (!isRecord(rawPackage) || typeof rawPackage.resolved !== 'string' || !rawPackage.resolved) {
        warnings.push(`Skipped NuGet package ${name}: exact resolved version is unavailable`);
        continue;
      }
      const key = `${name.toLowerCase()}@${rawPackage.resolved}`;
      const dependencies = isRecord(rawPackage.dependencies)
        ? Object.fromEntries(Object.keys(rawPackage.dependencies).map((dependency) => [dependency, '']))
        : {};
      const existing = records.get(key);
      records.set(key, {
        pkg: {
          name,
          version: rawPackage.resolved,
          integrity: stringValue(rawPackage.contentHash),
          dependencies: { ...(existing?.pkg.dependencies ?? {}), ...dependencies },
        },
        direct: existing?.direct === true || rawPackage.type === 'Direct' || rawPackage.type === 'Project',
      });
    }
  }
  const versionsByName = new Map<string, string[]>();
  for (const { pkg } of records.values()) {
    const versions = versionsByName.get(pkg.name.toLowerCase()) ?? [];
    versions.push(pkg.version);
    versionsByName.set(pkg.name.toLowerCase(), versions);
  }
  for (const { pkg } of records.values()) {
    pkg.dependencies = Object.fromEntries(Object.keys(pkg.dependencies ?? {}).flatMap((name) => {
      const versions = versionsByName.get(name.toLowerCase()) ?? [];
      if (versions.length !== 1) {
        warnings.push(`Could not uniquely resolve NuGet dependency ${name} declared by ${pkg.name}@${pkg.version}`);
        return [];
      }
      return [[name, versions[0]]];
    }));
  }
  return snapshot({
    ecosystem: 'NuGet',
    packageManager: 'nuget',
    source: 'packages.lock.json',
    rootDependencies: Object.fromEntries(
      [...records.values()].filter(({ direct }) => direct).map(({ pkg }) => [pkg.name, pkg.version]),
    ),
    packages: [...records.values()].map(({ pkg }) => pkg),
    warnings,
  });
}

export function parseYarnLock(text: string, packageJsonText?: string): Lockfile {
  let data: unknown;
  try {
    data = parseSyml(text);
  } catch {
    throw new Error('yarn.lock is not valid Yarn lock syntax.');
  }
  if (!isRecord(data)) throw new Error('yarn.lock does not contain package entries.');
  const warnings: string[] = [];
  const records = new Map<string, PackageInput>();
  for (const [descriptors, rawEntry] of Object.entries(data)) {
    if (descriptors === '__metadata' || !isRecord(rawEntry)) continue;
    const version = stringValue(rawEntry.version);
    const name = yarnDescriptorName(descriptors.split(',')[0].trim());
    if (!name || !version) {
      warnings.push(`Skipped Yarn entry ${descriptors}: package name or exact version is unavailable`);
      continue;
    }
    const dependencies = isRecord(rawEntry.dependencies)
      ? Object.fromEntries(Object.keys(rawEntry.dependencies).map((dependency) => [dependency, '']))
      : {};
    records.set(`${name}@${version}`, {
      name,
      version,
      integrity: stringValue(rawEntry.integrity) ?? stringValue(rawEntry.checksum),
      dependencies,
    });
  }
  resolveUniqueDependencyVersions(records, warnings, 'Yarn');
  const manifest = parseJsonObject(packageJsonText, 'package.json', warnings);
  const roots = rootDependenciesFromManifest(manifest, records, warnings, 'Yarn');
  return snapshot({
    projectName: stringValue(manifest.name),
    ecosystem: 'npm',
    packageManager: 'yarn',
    source: 'yarn.lock',
    rootDependencies: roots.prod,
    rootDevDependencies: roots.dev,
    packages: [...records.values()],
    warnings,
  });
}

export function parseBunLock(text: string): Lockfile {
  const errors: ParseError[] = [];
  const data = parseJsonc(text, errors, { allowTrailingComma: true, disallowComments: false }) as unknown;
  if (errors.length || !isRecord(data) || !isRecord(data.packages)) {
    throw new Error('bun.lock is not valid Bun JSONC lock syntax.');
  }
  const warnings: string[] = [];
  const records = new Map<string, PackageInput>();
  for (const [key, rawEntry] of Object.entries(data.packages)) {
    if (!Array.isArray(rawEntry) || typeof rawEntry[0] !== 'string') {
      warnings.push(`Skipped Bun package ${key}: package identity is unavailable`);
      continue;
    }
    const identity = packageIdentity(rawEntry[0]);
    if (!identity) {
      warnings.push(`Skipped Bun package ${key}: exact registry version is unavailable`);
      continue;
    }
    const dependencies = isRecord(rawEntry[2])
      ? Object.fromEntries(Object.keys(rawEntry[2]).map((dependency) => [dependency, '']))
      : {};
    records.set(`${identity.name}@${identity.version}`, {
      ...identity,
      dependencies,
      integrity: typeof rawEntry[3] === 'string' ? rawEntry[3] : undefined,
    });
  }
  resolveUniqueDependencyVersions(records, warnings, 'Bun');
  const workspaces = isRecord(data.workspaces) ? data.workspaces : {};
  const root = isRecord(workspaces['']) ? workspaces[''] : {};
  const roots = rootDependenciesFromManifest(root, records, warnings, 'Bun');
  return snapshot({
    projectName: stringValue(root.name),
    ecosystem: 'npm',
    packageManager: 'bun',
    source: 'bun.lock',
    rootDependencies: roots.prod,
    rootDevDependencies: roots.dev,
    packages: [...records.values()],
    warnings,
  });
}

export function parseGemfileLock(text: string): Lockfile {
  const lines = text.split(/\r?\n/);
  const records = new Map<string, PackageInput>();
  const warnings: string[] = [];
  let section = '';
  let inSpecs = false;
  let current: PackageInput | undefined;
  const directNames: string[] = [];
  for (const line of lines) {
    if (/^[A-Z][A-Z ]+$/.test(line)) {
      section = line.trim();
      inSpecs = false;
      current = undefined;
      continue;
    }
    if (section === 'GEM' && line.trim() === 'specs:') { inSpecs = true; continue; }
    if (section === 'GEM' && inSpecs) {
      const packageMatch = /^ {4}([^\s(]+) \(([^)\s]+)(?: [^)]+)?\)$/.exec(line);
      if (packageMatch) {
        current = { name: packageMatch[1], version: packageMatch[2], dependencies: {} };
        records.set(`${current.name}@${current.version}`, current);
        continue;
      }
      const dependencyMatch = /^ {6}([^\s(]+)(?: \([^)]+\))?$/.exec(line);
      if (dependencyMatch && current) current.dependencies![dependencyMatch[1]] = '';
    } else if (section === 'DEPENDENCIES') {
      const directMatch = /^  ([^\s!(]+)(?:[ !].*)?$/.exec(line);
      if (directMatch) directNames.push(directMatch[1]);
    }
  }
  if (records.size === 0) throw new Error('Gemfile.lock does not contain GEM specs.');
  resolveUniqueDependencyVersions(records, warnings, 'Bundler');
  const byName = versionsByName(records);
  const rootDependencies = Object.fromEntries(directNames.flatMap((name) => {
    const versions = byName.get(name) ?? [];
    if (versions.length === 1) return [[name, versions[0]]];
    warnings.push(`Could not uniquely resolve direct gem ${name}`);
    return [];
  }));
  return snapshot({
    ecosystem: 'RubyGems',
    packageManager: 'bundler',
    source: 'Gemfile.lock',
    rootDependencies,
    packages: [...records.values()],
    warnings,
  });
}

export function parsePubspecLock(text: string): Lockfile {
  let data: unknown;
  try { data = parseYaml(text); } catch { throw new Error('pubspec.lock is not valid YAML.'); }
  if (!isRecord(data) || !isRecord(data.packages)) throw new Error('pubspec.lock must contain a packages object.');
  const packages: PackageInput[] = [];
  const prod: Record<string, string> = {};
  const dev: Record<string, string> = {};
  const warnings = ['pubspec.lock does not record dependency edges; dependency paths are unavailable.'];
  for (const [name, rawPackage] of Object.entries(data.packages)) {
    if (!isRecord(rawPackage) || typeof rawPackage.version !== 'string' || !isExactVersion(rawPackage.version)) {
      warnings.push(`Skipped Pub package ${name}: exact version is unavailable`);
      continue;
    }
    packages.push({ name, version: rawPackage.version });
    if (rawPackage.dependency === 'direct main') prod[name] = rawPackage.version;
    if (rawPackage.dependency === 'direct dev') dev[name] = rawPackage.version;
  }
  return snapshot({
    ecosystem: 'Pub',
    packageManager: 'pub',
    source: 'pubspec.lock',
    rootDependencies: prod,
    rootDevDependencies: dev,
    packages,
    warnings,
  });
}

export function parseSwiftResolved(text: string): Lockfile {
  let data: unknown;
  try { data = JSON.parse(text); } catch { throw new Error('Package.resolved is not valid JSON.'); }
  if (!isRecord(data)) throw new Error('Package.resolved must contain a JSON object.');
  const objectPins = isRecord(data.object) ? data.object.pins : undefined;
  const pins = Array.isArray(data.pins) ? data.pins : Array.isArray(objectPins) ? objectPins : undefined;
  if (!pins) throw new Error('Package.resolved must contain a pins array.');
  const warnings = ['Package.resolved does not identify direct dependencies or dependency edges.'];
  const packages: PackageInput[] = [];
  for (const rawPin of pins) {
    if (!isRecord(rawPin) || !isRecord(rawPin.state)) continue;
    const name = stringValue(rawPin.identity) ?? stringValue(rawPin.package);
    const version = stringValue(rawPin.state.version);
    if (!name || !version || !isSemanticVersion(version)) {
      warnings.push(`Skipped Swift package ${name ?? '(unknown)'}: a semantic version is unavailable`);
      continue;
    }
    packages.push({ name, version, integrity: stringValue(rawPin.state.revision) });
  }
  return snapshot({
    ecosystem: 'SwiftURL',
    packageManager: 'swift-package-manager',
    source: 'Package.resolved',
    rootDependencies: {},
    packages,
    warnings,
  });
}

function snapshot(input: SnapshotInput): Lockfile {
  const packages: Record<string, unknown> = {
    '': { dependencies: input.rootDependencies, devDependencies: input.rootDevDependencies ?? {} },
  };
  input.packages.forEach((pkg, index) => {
    packages[`packages/${encodeURIComponent(pkg.name)}@${encodeURIComponent(pkg.version)}#${index}`] = {
      name: pkg.name,
      version: pkg.version,
      dependencies: pkg.dependencies ?? {},
      integrity: pkg.integrity,
    };
  });
  return {
    name: input.projectName,
    lockfileVersion: 3,
    packages,
    provenyxSource: input.source,
    provenyxEcosystem: input.ecosystem,
    provenyxPackageManager: input.packageManager,
    provenyxWarnings: input.warnings ?? [],
  };
}

function pythonSection(section: UnknownRecord, warnings: string[]): PackageInput[] {
  return Object.entries(section).flatMap(([name, rawValue]) => {
    const value = typeof rawValue === 'string' ? rawValue : isRecord(rawValue) ? rawValue.version : undefined;
    const match = typeof value === 'string' ? /^==(.+)$/.exec(value) : undefined;
    if (!match || !isExactVersion(match[1])) {
      warnings.push(`Skipped ${name}: Pipfile.lock entry does not contain an exact == version`);
      return [];
    }
    const hashes = isRecord(rawValue) ? rawValue.hashes : undefined;
    const integrity = Array.isArray(hashes) ? hashes.find((hash): hash is string => typeof hash === 'string') : undefined;
    return [{ name, version: match[1], integrity }];
  });
}

function parseGoModRequires(text: string): Record<string, string> {
  const requires: Record<string, string> = {};
  let inBlock = false;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.replace(/\/\/.*$/, '').trim();
    if (line === 'require (') { inBlock = true; continue; }
    if (inBlock && line === ')') { inBlock = false; continue; }
    const body = inBlock ? line : line.startsWith('require ') ? line.slice(8).trim() : '';
    const [name, version] = body.split(/\s+/);
    if (name && version) requires[name] = version;
  }
  return requires;
}

type CargoPackage = PackageInput & { source?: string; rawDependencies?: string[] };

function parseCargoPackage(block: string): CargoPackage | undefined {
  const value = (key: string) => {
    const match = new RegExp(`^\\s*${key}\\s*=\\s*("(?:[^"\\\\]|\\\\.)*")`, 'm').exec(block);
    if (!match) return undefined;
    try { return JSON.parse(match[1]) as string; } catch { return undefined; }
  };
  const name = value('name');
  const version = value('version');
  if (!name || !version) return undefined;
  const dependenciesMatch = /^\s*dependencies\s*=\s*\[([\s\S]*?)\]/m.exec(block);
  const rawDependencies = dependenciesMatch
    ? [...dependenciesMatch[1].matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((match) => JSON.parse(`"${match[1]}"`) as string)
    : [];
  return { name, version, source: value('source'), integrity: value('checksum'), rawDependencies };
}

function isPackageInput(value: CargoPackage | undefined): value is CargoPackage {
  return value !== undefined;
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | undefined {
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return String(value);
  return undefined;
}

function isExactVersion(value: string): boolean {
  return value.length > 0 && !/[\s*?]/.test(value) && !/^(?:[<>=~^]|latest$)/i.test(value);
}

function isSemanticVersion(value: string): boolean {
  return /^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(value);
}

function parseJsonObject(text: string | undefined, label: string, warnings: string[]): UnknownRecord {
  if (text === undefined) {
    warnings.push(`${label} was not found; direct dependencies are unknown.`);
    return {};
  }
  try {
    const value = JSON.parse(text) as unknown;
    if (isRecord(value)) return value;
  } catch {
    // Report the same conservative outcome for malformed or non-object manifests.
  }
  warnings.push(`${label} is invalid; direct dependencies are unknown.`);
  return {};
}

function yarnDescriptorName(descriptor: string): string | undefined {
  if (descriptor.startsWith('@')) {
    const separator = descriptor.indexOf('@', descriptor.indexOf('/') + 1);
    return separator > 0 ? descriptor.slice(0, separator) : undefined;
  }
  const separator = descriptor.indexOf('@');
  return separator > 0 ? descriptor.slice(0, separator) : undefined;
}

function packageIdentity(value: string): { name: string; version: string } | undefined {
  const separator = value.startsWith('@') ? value.indexOf('@', value.indexOf('/') + 1) : value.lastIndexOf('@');
  if (separator <= 0) return undefined;
  const name = value.slice(0, separator);
  const version = value.slice(separator + 1);
  return name && version && !/^(?:workspace|file|link|git)/.test(version) ? { name, version } : undefined;
}

function versionsByName(records: Map<string, PackageInput>): Map<string, string[]> {
  const result = new Map<string, string[]>();
  for (const pkg of records.values()) {
    const versions = result.get(pkg.name) ?? [];
    if (!versions.includes(pkg.version)) versions.push(pkg.version);
    result.set(pkg.name, versions);
  }
  return result;
}

function resolveUniqueDependencyVersions(records: Map<string, PackageInput>, warnings: string[], label: string): void {
  const byName = versionsByName(records);
  for (const pkg of records.values()) {
    pkg.dependencies = Object.fromEntries(Object.keys(pkg.dependencies ?? {}).flatMap((name) => {
      const versions = byName.get(name) ?? [];
      if (versions.length !== 1) {
        warnings.push(`Could not uniquely resolve ${label} dependency ${name} declared by ${pkg.name}@${pkg.version}`);
        return [];
      }
      return [[name, versions[0]]];
    }));
  }
}

function rootDependenciesFromManifest(
  manifest: UnknownRecord,
  records: Map<string, PackageInput>,
  warnings: string[],
  label: string,
): { prod: Record<string, string>; dev: Record<string, string> } {
  const byName = versionsByName(records);
  const resolve = (value: unknown) => {
    if (!isRecord(value)) return {};
    return Object.fromEntries(Object.keys(value).flatMap((name) => {
      const versions = byName.get(name) ?? [];
      if (versions.length === 1) return [[name, versions[0]]];
      warnings.push(`Could not uniquely resolve direct ${label} package ${name}`);
      return [];
    }));
  };
  return {
    prod: { ...resolve(manifest.dependencies), ...resolve(manifest.optionalDependencies) },
    dev: resolve(manifest.devDependencies),
  };
}