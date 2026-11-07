import type { Metadata } from 'next'
import { BackendStatus } from '@/components/backend-status'
import { PageHeader } from '@/components/page-header'
import { API_BASE_URL } from '@/lib/config'

export const metadata: Metadata = { title: 'Settings' }

const EXTENSION_SETTINGS = [
  { key: 'provenyx.apiBaseUrl', value: 'https://devknights.onrender.com', note: 'Where lockfiles are sent after you confirm a scan. Use http://localhost:8000 for a local API.' },
  { key: 'provenyx.dashboardUrl', value: 'https://provenyx.vercel.app', note: 'Opened by “Provenyx: Open Dashboard”.' },
  { key: 'provenyx.autoScanEnabled', value: 'true', note: 'Offer a scan when a supported dependency file changes. Still asks before sending.' },
]

function Code({ children }: { children: React.ReactNode }) {
  return <code className="rounded bg-surface px-1.5 py-0.5 text-[13px] text-ink">{children}</code>
}

export default function SettingsPage() {
  return (
    <div className="flex flex-col gap-8">
      <PageHeader eyebrow="Configuration" title="Settings" description="Connection details for this local development setup." />

      <section aria-labelledby="api-title" className="flex flex-col gap-3">
        <h2 id="api-title" className="font-semibold text-ink">API connection</h2>
        <p className="text-sm text-ink-muted">
          The dashboard calls <Code>{API_BASE_URL}</Code>. Change it with <Code>NEXT_PUBLIC_PROVENYX_API_URL</Code> in{' '}
          <Code>frontend/.env.local</Code>.
        </p>
        <BackendStatus />
      </section>

      <section id="extension" aria-labelledby="extension-title" className="scroll-mt-8 rounded-xl border border-line bg-panel p-6">
        <h2 id="extension-title" className="font-semibold text-ink">VS Code extension setup</h2>
        <ol className="mt-4 flex list-decimal flex-col gap-2 pl-5 text-sm leading-6 text-ink">
          <li>
            Start the API: in <Code>backend/</Code> run <Code>uvicorn app.main:app --port 8000</Code>.
          </li>
          <li>
            Install <strong>Provenyx Security</strong> (<Code>OmkarPramodBandikatte.provenyx-security</Code>) from the Marketplace, or
            open the repository in VS Code and press <Code>F5</Code> (“Run Provenyx Extension”). Uninstall the obsolete{' '}
            <Code>Provenyx.provenyx-security</Code> if it is installed.
          </li>
          <li>
            Open the Provenyx view in the Activity Bar and check that the backend shows <strong>Connected</strong>.
          </li>
          <li>
            Run <strong>Provenyx: Scan Dependencies</strong> and confirm the prompt.
          </li>
        </ol>
        <div className="relative mt-6 overflow-x-auto">
          <table className="w-full min-w-[520px] text-left text-sm">
            <caption className="sr-only">VS Code extension settings</caption>
            <thead className="text-[11px] uppercase tracking-wider text-ink-muted">
              <tr>
                <th scope="col" className="py-2 pr-4 font-semibold">Setting</th>
                <th scope="col" className="py-2 pr-4 font-semibold">Default</th>
                <th scope="col" className="py-2 font-semibold">Purpose</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {EXTENSION_SETTINGS.map((s) => (
                <tr key={s.key}>
                  <td className="py-3 pr-4 font-mono text-[13px] text-ink">{s.key}</td>
                  <td className="py-3 pr-4 font-mono text-[13px] text-ink-muted">{s.value}</td>
                  <td className="py-3 text-ink-muted">{s.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
