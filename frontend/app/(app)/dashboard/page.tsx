import type { Metadata } from 'next'
import Link from 'next/link'
import { ArrowRight, Code2, Plus } from 'lucide-react'
import { BackendStatus } from '@/components/backend-status'
import { DashboardOverview } from '@/components/dashboard-overview'
import { PageHeader, primaryButton, secondaryButton } from '@/components/page-header'

export const metadata: Metadata = { title: 'Dashboard' }

const STEPS = [
  'Install the Provenyx Security extension in VS Code.',
  'Open a project that contains a supported dependency file.',
  'Run “Provenyx: Scan Dependencies” from the Command Palette.',
  'Confirm the prompt to send the lockfile to your Provenyx API.',
]

export default function DashboardPage() {
  return (
    <div className="flex flex-col gap-8">
      <PageHeader
        eyebrow="Development workspace"
        title="Dashboard"
        description="Investigate dependency trust for your npm projects."
        action={<Link href="/scan/new" className={primaryButton}><Plus className="size-4" aria-hidden /> New scan</Link>}
      />

      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <section className="rounded-xl border border-line bg-panel p-6" aria-labelledby="onboarding-title">
          <div className="flex items-start gap-4">
            <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-brand text-white">
              <Code2 className="size-5" aria-hidden />
            </span>
            <div>
              <h2 id="onboarding-title" className="font-semibold text-ink">Scan from VS Code</h2>
              <p className="mt-1 text-sm text-ink-muted">Provenyx reads your lockfile. It never installs or runs your packages.</p>
            </div>
          </div>
          <ol className="mt-6 flex flex-col gap-3">
            {STEPS.map((step, i) => (
              <li key={step} className="flex items-start gap-3 text-sm text-ink">
                <span className="grid size-6 shrink-0 place-items-center rounded-full bg-mint-soft text-xs font-semibold text-leaf">{i + 1}</span>
                <span className="pt-0.5">{step}</span>
              </li>
            ))}
          </ol>
          <Link href="/settings#extension" className={`${secondaryButton} mt-6`}>
            Extension setup instructions <ArrowRight className="size-4" aria-hidden />
          </Link>
        </section>

        <BackendStatus />
      </div>

      <DashboardOverview />
    </div>
  )
}
