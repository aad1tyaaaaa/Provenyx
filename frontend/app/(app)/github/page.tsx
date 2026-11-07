import type { Metadata } from 'next'
import { GitHubProjects } from '@/components/github-projects'
import { PageHeader } from '@/components/page-header'

export const metadata: Metadata = { title: 'GitHub projects' }

export default function GitHubProjectsPage() {
  return (
    <div className="flex flex-col gap-8">
      <PageHeader
        eyebrow="Repository access"
        title="GitHub projects"
        description="Choose any accessible repository and analyze a supported dependency file with the same Provenyx pipeline used by local scans."
      />
      <GitHubProjects />
    </div>
  )
}