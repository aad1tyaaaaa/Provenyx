'use client'

import { useId, useState } from 'react'
import Link from 'next/link'
import { ArrowRight, Check, FileCode2, LoaderCircle, Upload, X } from 'lucide-react'
import { primaryButton, secondaryButton } from '@/components/page-header'
import { ErrorState } from '@/components/states'
import { apiPost } from '@/lib/api'
import { API_BASE_URL } from '@/lib/config'
import { formatBytes } from '@/lib/format'
import { DEPENDENCY_FILE_ACCEPT, parseDependencyFiles, type ParsedDependencyFiles } from '@/lib/lockfile'
import type { ScanAccepted } from '@/lib/types'
import { cn } from '@/lib/utils'

type Selected = ParsedDependencyFiles
type Submit = { state: 'idle' } | { state: 'sending' } | { state: 'done'; res: ScanAccepted } | { state: 'error'; message: string }

export function ScanDropzone() {
  const inputId = useId()
  const [dragging, setDragging] = useState(false)
  const [file, setFile] = useState<Selected | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [consent, setConsent] = useState(false)
  const [submit, setSubmit] = useState<Submit>({ state: 'idle' })

  async function choose(files: File[]) {
    setFile(null)
    setFileError(null)
    setSubmit({ state: 'idle' })
    if (files.length === 0) return
    try {
      setFile(await parseDependencyFiles(files))
    } catch (err) {
      setFileError((err as Error).message)
    }
  }

  async function send() {
    if (!file || !consent) return
    setSubmit({ state: 'sending' })
    const r = await apiPost<ScanAccepted>('/api/dependency-scans', {
      filename: file.filename,
      content: file.content,
      auxiliaries: file.auxiliaries,
      client: 'dashboard/0.1.0',
    })
    if (r.state === 'ok') setSubmit({ state: 'done', res: r.data })
    else setSubmit({ state: 'error', message: r.state === 'error' ? r.message : 'The API does not accept scans at this address.' })
  }

  if (submit.state === 'done')
    return (
      <section className="rounded-xl border border-line bg-panel p-6" aria-live="polite">
        <div className="flex items-start gap-3">
          <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-mint-soft text-leaf"><Check className="size-5" aria-hidden /></span>
          <div className="min-w-0">
            <h2 className="font-semibold text-ink">Scan accepted</h2>
            <p className="mt-1 text-sm text-ink-muted">{submit.res.message}</p>
            <p className="mt-2 break-all font-mono text-xs text-ink-muted">Scan ID: {submit.res.scan_id}</p>
          </div>
        </div>
        <div className="mt-6 flex flex-wrap gap-3">
          <Link href={`/scans/${submit.res.scan_id}`} className={primaryButton}>View scan <ArrowRight className="size-4" aria-hidden /></Link>
          <button type="button" className={secondaryButton} onClick={() => { setFile(null); setConsent(false); setSubmit({ state: 'idle' }) }}>
            Scan another lockfile
          </button>
        </div>
      </section>
    )

  return (
    <section className="rounded-xl border border-line bg-panel p-6 sm:p-8">
      <div className="flex items-start gap-4">
        <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-mint-soft text-leaf"><Upload className="size-5" aria-hidden /></span>
        <div>
          <h2 className="font-semibold text-ink">Upload a dependency file</h2>
          <p className="mt-1 text-sm text-ink-muted">Choose one supported dependency file and optional adjacent manifest. Nothing is sent until you confirm below.</p>
        </div>
      </div>

      <label
        htmlFor={inputId}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); void choose(Array.from(e.dataTransfer.files)) }}
        className={cn(
          'mt-6 flex min-h-40 cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed px-6 text-center transition focus-within:outline-2 focus-within:outline-leaf',
          dragging ? 'border-leaf bg-ink/5' : 'border-ink/20 bg-ink/5 hover:border-leaf hover:bg-ink/5',
        )}
      >
        <input id={inputId} className="sr-only" type="file" multiple accept={DEPENDENCY_FILE_ACCEPT} onChange={(e) => void choose(Array.from(e.target.files ?? []))} />
        <FileCode2 className="size-6 text-leaf" aria-hidden />
        <span className="mt-3 text-sm font-semibold text-ink/70">Drop a supported dependency file here, or choose files</span>
        <span className="mt-1 text-xs text-ink-muted">Dependency file plus optional manifest, up to 10 MB total</span>
      </label>

      {fileError && <div className="mt-4"><ErrorState title="This file can't be scanned">{fileError}</ErrorState></div>}

      {file && (
        <div className="mt-4 flex items-center justify-between gap-3 rounded-lg bg-ink/5 px-4 py-3 text-sm">
          <div className="min-w-0">
            <p className="truncate font-semibold text-ok">{file.filename}</p>
            <p className="text-xs text-ink-muted">
              {formatBytes(file.totalSize)}{file.packageCount !== undefined ? ` · ${file.packageCount} package entries` : ''}
              {Object.keys(file.auxiliaries).length > 0 ? ` · ${Object.keys(file.auxiliaries).join(', ')}` : ''}
            </p>
          </div>
          <button type="button" onClick={() => { setFile(null); setConsent(false) }} className="rounded-md p-1.5 text-ink-muted hover:bg-ink/10 hover:text-ink" aria-label="Remove file">
            <X className="size-4" aria-hidden />
          </button>
        </div>
      )}

      <div className="mt-6 border-t border-line pt-6">
        <label className="flex items-start gap-3 text-sm text-ink">
          <input type="checkbox" checked={consent} disabled={!file} onChange={(e) => setConsent(e.target.checked)} className="mt-1 size-4 accent-[#a8325f]" />
          <span>
            Send this lockfile to <code className="rounded bg-surface px-1.5 py-0.5 text-[13px]">{API_BASE_URL}</code> for analysis.
            <span className="block text-xs text-ink-muted">Only the lockfile is sent. Provenyx never installs or runs packages.</span>
          </span>
        </label>
        {submit.state === 'error' && <div className="mt-4"><ErrorState title="Scan was not submitted">{submit.message}</ErrorState></div>}
        <div className="mt-6 flex justify-end">
          <button type="button" className={primaryButton} disabled={!file || !consent || submit.state === 'sending'} onClick={() => void send()}>
            {submit.state === 'sending' ? <LoaderCircle className="size-4 animate-spin" aria-hidden /> : null}
            Start scan
          </button>
        </div>
      </div>
    </section>
  )
}
