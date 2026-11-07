// Same rules as extension/src/utils/workspace.ts and the backend's Lockfile model.

export const MAX_LOCKFILE_BYTES = 10 * 1024 * 1024

export const SUPPORTED_DEPENDENCY_FILES = [
  'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'composer.lock',
  'Pipfile.lock', 'requirements.txt', 'go.sum', 'Cargo.lock', 'pom.xml',
  'gradle.lockfile', 'packages.lock.json', 'Gemfile.lock', 'pubspec.lock', 'Package.resolved',
] as const

export const AUXILIARY_MANIFESTS = ['package.json', 'composer.json', 'go.mod'] as const
export const DEPENDENCY_FILE_ACCEPT = [...SUPPORTED_DEPENDENCY_FILES, ...AUXILIARY_MANIFESTS].join(',')

export interface ParsedDependencyFiles {
  filename: string
  content: string
  auxiliaries: Record<string, string>
  totalSize: number
  packageCount?: number
}

export interface ParsedLockfile {
  data: Record<string, unknown> & { lockfileVersion: number; name?: string }
  packageCount: number
}

/** Parses and structurally validates package-lock.json text. Throws an Error with a user-facing message. */
export function parseLockfile(text: string): ParsedLockfile {
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    throw new Error('This file is not valid JSON.')
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new Error('A package-lock.json must contain a JSON object.')
  }
  const lock = data as ParsedLockfile['data']
  if (![1, 2, 3].includes(lock.lockfileVersion)) {
    throw new Error(`Unsupported lockfileVersion: ${String(lock.lockfileVersion)}. Expected 1, 2 or 3.`)
  }
  const section = lock.lockfileVersion === 1 ? 'dependencies' : 'packages'
  const value = lock[section]
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`lockfileVersion ${lock.lockfileVersion} requires a "${section}" object.`)
  }
  // v2/v3: the "" key is the project itself, not a dependency.
  const packageCount = Object.keys(value).filter((k) => k !== '').length
  return { data: lock, packageCount }
}

export async function parseDependencyFiles(files: File[]): Promise<ParsedDependencyFiles> {
  const supported = new Set<string>(SUPPORTED_DEPENDENCY_FILES)
  const auxiliaryNames = new Set<string>(AUXILIARY_MANIFESTS)
  const primary = files.filter((file) => supported.has(file.name))
  const invalid = files.find((file) => !supported.has(file.name) && !auxiliaryNames.has(file.name))
  if (invalid) throw new Error(`${invalid.name} is not a supported dependency file.`)
  if (primary.length !== 1) throw new Error('Choose exactly one supported dependency file.')
  const totalSize = files.reduce((total, file) => total + file.size, 0)
  if (totalSize > MAX_LOCKFILE_BYTES) throw new Error('The selected files exceed the 10 MB limit.')
  const duplicate = files.find((file, index) => files.findIndex((other) => other.name === file.name) !== index)
  if (duplicate) throw new Error(`${duplicate.name} was selected more than once.`)

  const content = await primary[0].text()
  if (!content.trim()) throw new Error(`${primary[0].name} is empty.`)
  let packageCount: number | undefined
  if (primary[0].name === 'package-lock.json') packageCount = parseLockfile(content).packageCount
  const jsonFiles = new Set(['composer.lock', 'Pipfile.lock', 'packages.lock.json', 'Package.resolved'])
  if (jsonFiles.has(primary[0].name)) {
    try { JSON.parse(content) } catch { throw new Error(`${primary[0].name} is not valid JSON.`) }
  }
  const auxiliaries = Object.fromEntries(
    await Promise.all(files.filter((file) => auxiliaryNames.has(file.name)).map(async (file) => [file.name, await file.text()])),
  )
  return { filename: primary[0].name, content, auxiliaries, totalSize, packageCount }
}
