import { Server } from 'lucide-react'
import { useVersion } from '@/lib/useSharedQueries'

export function SettingsAboutPanel() {
  const { data: versionData } = useVersion()

  return (
    <div className="pb-8 flex flex-col gap-6">
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center gap-2 mb-4">
          <Server className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">本地部署</h3>
        </div>
        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">当前版本</div>
            <div className="text-[11px] text-muted">已安装的应用版本，更新由本地部署维护</div>
          </div>
          <span className="font-mono text-xs text-secondary shrink-0">
            {versionData?.version ?? '—'}
          </span>
        </div>
      </section>
    </div>
  )
}
