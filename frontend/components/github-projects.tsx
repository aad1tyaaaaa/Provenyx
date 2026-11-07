'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { ExternalLink, FileCode2, GitFork, LoaderCircle, Lock, LogOut, Search } from 'lucide-react'
import { primaryButton, secondaryButton } from '@/components/page-header'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import { apiFetch, apiPost, useApi } from '@/lib/api'
import { signInHref, signOut } from '@/components/github-auth'
import type { GitHubAuthStatus, GitHubRepository, ScanAccepted } from '@/lib/types'

type Discovery =
  | { repository: string; state: 'loading' }
  | { repository: string; state: 'ok'; paths: string[] }
  | { repository: string; state: 'error'; message: string }
  | null

export function GitHubProjects() {
  const router = useRouter()
  const [auth, reloadAuth] = useApi<GitHubAuthStatus>('/api/github/auth/status')
  const connected = auth.state === 'ok' && auth.data.connected
  const [repositories] = useApi<GitHubRepository[]>(connected ? '/api/github/repositories' : null)
  const [query, setQuery] = useState('')
  const [discovery, setDiscovery] = useState<Discovery>(null)
  const [scanning, setScanning] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    const error = new URLSearchParams(window.location.search).get('github_error')
    if (error) setActionError(`GitHub authorization failed: ${error}`)
  }, [])

  async function discover(repository: GitHubRepository) {
    setActionError(null)
    setDiscovery({ repository: repository.full_name, state: 'loading' })
    const [owner, name] = repository.full_name.split('/', 2)
    const result = await apiFetch<string[]>(
      `/api/github/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/lockfiles`,
    )
    if (result.state === 'ok') setDiscovery({ repository: repository.full_name, state: 'ok', paths: result.data })
    else setDiscovery({ repository: repository.full_name, state: 'error', message: result.state === 'error' ? result.message : 'Lockfile discovery is unavailable.' })
  }

  async function scan(repository: GitHubRepository, lockfilePath: string) {
    const [owner, name] = repository.full_name.split('/', 2)
    const key = `${repository.full_name}:${lockfilePath}`
    setActionError(null)
    setScanning(key)
    const result = await apiPost<ScanAccepted>('/api/github/scans', {
      owner,
      repository: name,
      lockfile_path: lockfilePath,
    })
    setScanning(null)
    if (result.state === 'ok') router.push(`/scans/${result.data.scan_id}`)
    else setActionError(result.state === 'error' ? result.message : 'GitHub scanning is unavailable.')
  }

  async function disconnect() {
    await signOut()
    setDiscovery(null)
    reloadAuth()
  }

  if (auth.state === 'loading') return <LoadingState label="Checking GitHub connection…" />
  if (auth.state === 'error') return <ErrorState title="Could not check GitHub connection">{auth.message}</ErrorState>
  if (auth.state === 'unavailable') {
    return <ErrorState title="GitHub integration is unavailable">The connected Provenyx API does not provide GitHub authentication.</ErrorState>
  }
  if (!auth.data.configured) {
    return (
      <EmptyState icon={<GitFork className="size-5" aria-hidden />} title="GitHub OAuth needs configuration">
        Add the GitHub OAuth client ID, client secret, and session secret to the backend environment, then restart the API.
      </EmptyState>
    )
  }
  if (!auth.data.connected) {
    return (
      <div className="space-y-4">
        {actionError && <ErrorState title="Could not connect GitHub">{actionError}</ErrorState>}
        <EmptyState icon={<GitFork className="size-5" aria-hidden />} title="Connect your GitHub account">
          <p>Authorize repository access. Provenyx itself only performs read requests, retrieves the lockfile you choose, and never runs repository code.</p>
          <a className={`${primaryButton} mt-5`} href={signInHref('/github')}>
            <GitFork className="size-4" aria-hidden /> Connect GitHub
          </a>
        </EmptyState>
      </div>
    )
  }
  if (repositories.state === 'loading') return <LoadingState label="Loading GitHub projects…" />
  if (repositories.state === 'error') return <ErrorState title="Could not load GitHub projects">{repositories.message}</ErrorState>
  if (repositories.state === 'unavailable') return <ErrorState title="GitHub projects are unavailable">The repository endpoint is not available.</ErrorState>

  const normalizedQuery = query.trim().toLowerCase()
  const visible = repositories.data.filter((repository) =>
    `${repository.full_name} ${repository.description ?? ''}`.toLowerCase().includes(normalizedQuery),
  )

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 rounded-lg border border-line bg-panel p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          {auth.data.avatar_url && <img src={auth.data.avatar_url} alt="" className="size-9 rounded-full" />}
          <div className="min-w-0">
            <p className="text-xs text-ink-muted">Connected as</p>
            <p className="truncate text-sm font-semibold text-ink">{auth.data.login}</p>
          </div>
        </div>
        <button type="button" className={secondaryButton} onClick={() => void disconnect()}>
          <LogOut className="size-4" aria-hidden /> Disconnect
        </button>
      </div>

      {actionError && <ErrorState title="GitHub action failed">{actionError}</ErrorState>}

      <label className="relative block">
        <span className="sr-only">Search repositories</span>
        <Search className="pointer-events-none absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-ink-muted" aria-hidden />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search repositories"
          className="h-11 w-full rounded-lg border border-line bg-panel pl-10 pr-4 text-sm text-ink outline-none placeholder:text-ink-muted focus:border-leaf focus:ring-2 focus:ring-[#a8325f]/30"
        />
      </label>

      <p className="text-xs font-medium text-ink-muted">{visible.length} of {repositories.data.length} repositories</p>

      {visible.length === 0 ? (
        <EmptyState icon={<Search className="size-5" aria-hidden />} title="No matching repositories">Try a different repository name.</EmptyState>
      ) : (
        <div className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-panel">
          {visible.map((repository) => {
            const open = discovery?.repository === repository.full_name
            return (
              <section key={repository.id} className="p-4 sm:p-5">
                <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="break-all text-sm font-semibold text-ink">{repository.full_name}</h2>
                      {repository.private && <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase text-ink-muted"><Lock className="size-3" aria-hidden /> Private</span>}
                      <a href={repository.html_url} target="_blank" rel="noreferrer" aria-label={`Open ${repository.full_name} on GitHub`} className="text-ink-muted hover:text-ink">
                        <ExternalLink className="size-3.5" aria-hidden />
                      </a>
                    </div>
                    <p className="mt-1 line-clamp-2 text-xs leading-5 text-ink-muted">{repository.description ?? 'No description'}</p>
                  </div>
                  <button type="button" className={`${secondaryButton} shrink-0`} disabled={open && discovery.state === 'loading'} onClick={() => void discover(repository)}>
                    {open && discovery.state === 'loading' ? <LoaderCircle className="size-4 animate-spin" aria-hidden /> : <FileCode2 className="size-4" aria-hidden />}
                    Find lockfiles
                  </button>
                </div>

                {open && discovery.state === 'error' && <p className="mt-4 text-sm text-danger">{discovery.message}</p>}
                {open && discovery.state === 'ok' && discovery.paths.length === 0 && (
                  <p className="mt-4 text-sm text-ink-muted">No supported dependency file was found on {repository.default_branch}.</p>
                )}
                {open && discovery.state === 'ok' && discovery.paths.length > 0 && (
                  <div className="mt-4 space-y-2 border-t border-line pt-4">
                    {discovery.paths.map((path) => {
                      const key = `${repository.full_name}:${path}`
                      return (
                        <div key={path} className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                          <code className="min-w-0 break-all text-xs text-ink-muted">{path}</code>
                          <button type="button" className={`${primaryButton} shrink-0 px-3 py-2`} disabled={scanning !== null} onClick={() => void scan(repository, path)}>
                            {scanning === key && <LoaderCircle className="size-4 animate-spin" aria-hidden />}
                            Analyze
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>
            )
          })}
        </div>
      )}
    </div>
  )
}