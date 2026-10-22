import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDownLeft, ArrowUpRight, Landmark, RefreshCw, Search, Settings2 } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import type { FundFlowRow, FundFlowSubjectType } from '@/lib/api'
import { useCapabilityMatrix } from '@/lib/useSharedQueries'
import { useFundFlow } from '@/lib/useFundFlow'
import { fmtPct, fmtVolume, priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'

type SortField = 'net_amount' | 'inflow_amount' | 'outflow_amount'
const LABELS: Record<FundFlowSubjectType, string> = { industry: '行业', concept: '概念', stock: '个股' }
const FIELDS: { id: SortField; label: string }[] = [
  { id: 'net_amount', label: '净流入' }, { id: 'inflow_amount', label: '流入' }, { id: 'outflow_amount', label: '流出' },
]
const INPUT = 'h-9 rounded-btn border border-border bg-surface px-3 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent'

export function sortFundFlow(rows: FundFlowRow[], field: SortField, ascending: boolean) {
  return [...rows].sort((a, b) => {
    const av = a[field], bv = b[field]
    if (av == null && bv == null) return a.source_subject_key.localeCompare(b.source_subject_key)
    if (av == null) return 1
    if (bv == null) return -1
    return (ascending ? av - bv : bv - av) || a.source_subject_key.localeCompare(b.source_subject_key)
  })
}

function stamp(value: string | null) {
  return value ? new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(value)) : '来源未披露'
}

export function FundFlowPage() {
  const matrix = useCapabilityMatrix()
  const route = matrix.data?.capabilities.find(c => c.id === 'fund_flow')
  const caps = route?.details
  const [subjectType, setSubjectType] = useState<FundFlowSubjectType>('industry')
  const [windowId, setWindowId] = useState('today')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<SortField>('net_amount')
  const [ascending, setAscending] = useState(false)
  const type = caps?.subject_types.includes(subjectType) ? subjectType : caps?.subject_types[0] ?? 'industry'
  const window = caps?.windows.some(w => w.id === windowId) ? windowId : caps?.windows[0]?.id ?? 'today'
  const field = caps?.amount_fields.includes(sort) ? sort : caps?.amount_fields[0] ?? 'net_amount'
  const flow = useFundFlow({ subject_type: type, window_id: window }, route)
  const data = flow.data
  const rows = useMemo(() => sortFundFlow((data?.rows ?? []).filter(r =>
    `${r.name} ${r.subject_id ?? ''}`.toLowerCase().includes(search.trim().toLowerCase())), field, ascending), [data, search, field, ascending])
  const allRows = data?.rows ?? []
  const inflows = allRows.filter(r => r.net_amount != null && r.net_amount > 0)
  const outflows = allRows.filter(r => r.net_amount != null && r.net_amount < 0)
  const available = Boolean(route?.usable && caps)
  const hasNet = caps?.amount_fields.includes('net_amount') ?? false
  const hasData = Boolean(data?.complete)
  const settings = <Link to="/settings?tab=data-sources" className="inline-flex items-center gap-1.5 text-xs text-accent"><Settings2 className="h-3.5 w-3.5" />数据源设置</Link>

  return (
    <div className="flex h-full min-h-0 flex-col bg-base">
      <PageHeader title="资金流向" subtitle="在同一统计窗口，观察资金流入、流出与板块净额" right={settings} />
      <div className="flex-1 overflow-auto p-4 sm:p-5">
        <div className="mx-auto max-w-[1600px] space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <select aria-label="标的类型" className={INPUT} value={type} disabled={!available} onChange={e => setSubjectType(e.target.value as FundFlowSubjectType)}>
              {(caps?.subject_types ?? ['industry', 'concept']).map(t => <option key={t} value={t}>{LABELS[t]}</option>)}
            </select>
            <select aria-label="统计窗口" className={`${INPUT} max-w-[220px]`} value={window} disabled={!available} onChange={e => setWindowId(e.target.value)}>
              {(caps?.windows ?? [{ id: 'today', label: '当天（源快照）' }]).map(w => <option key={w.id} value={w.id}>{w.label}</option>)}
            </select>
            <label className="relative min-w-[150px] flex-1 sm:max-w-[280px]">
              <Search className="absolute left-3 top-3 h-3.5 w-3.5 text-muted" />
              <input aria-label="搜索板块" value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索名称或板块代码" className={`${INPUT} w-full pl-8`} />
            </label>
            <button disabled={!available || flow.isFetching} onClick={() => { void flow.refetch() }} className={`${INPUT} ml-auto inline-flex items-center gap-2 disabled:opacity-50`}>
              <RefreshCw className={cn('h-3.5 w-3.5', flow.isFetching && 'animate-spin')} />{flow.isFetching ? '更新中' : '刷新'}
            </button>
          </div>

          {matrix.isLoading ? <State title="正在检查资金流能力…" />
            : matrix.isError ? <State title="能力信息加载失败" detail={matrix.error.message} action={<button className="text-accent" onClick={() => { void matrix.refetch() }}>重新加载</button>} />
            : !available ? <State title="资金流数据源未就绪" detail={route?.pending.map(p => `${p.display}：${p.note ?? p.status}`).join('；') || '请选择支持资金流的数据源。'} action={settings} />
            : flow.isLoading ? <State title="正在获取完整资金流榜单…" detail="首次获取需要翻页并关联板块代码，请稍候。" />
            : !hasData ? <State title={data?.status === 'no_data' ? '当前窗口暂无数据' : '资金流暂不可用'} detail={flow.error?.message ?? data?.message ?? '所选窗口或粒度尚未获得完整数据。'} />
            : <>
              {(data?.status === 'stale' || flow.isError) && <div role="status" className="rounded-btn border border-warning/30 bg-warning/5 px-4 py-3 text-xs text-warning">{data?.message ?? '更新失败，保留上次完整数据'} · 以下数据已过期</div>}
              <div className="flex flex-wrap gap-x-5 gap-y-1 text-[11px] leading-5 text-muted">
                <span>来源：{route?.effective_display}</span><span>{caps?.metric_label} · {caps?.taxonomy}</span>
                <span>抓取：{stamp(data?.fetched_at ?? null)}</span><span>源更新时间：{stamp(data?.source_updated_at ?? null)}</span>
                <span>交易时段每 {caps?.refresh_interval_s ?? 60} 秒检查更新</span>
              </div>
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <Metric label={`覆盖${LABELS[type]}`} value={`${allRows.length}`} />
                <Metric label="净流入数量" value={hasNet ? `${inflows.length}` : '—'} color="text-bull" />
                <Metric label="净流出数量" value={hasNet ? `${outflows.length}` : '—'} color="text-bear" />
                <Metric label="窗口" value={data?.window?.label ?? '—'} />
              </div>
              {allRows.length === 0 ? <State title="当前窗口暂无数据" detail="数据源已返回完整空结果，可切换窗口查看。" /> : <>
                {hasNet && <div className="grid gap-4 xl:grid-cols-2">
                  <Ranking title="净流入前 10" rows={sortFundFlow(inflows, 'net_amount', false).slice(0, 10)} positive />
                  <Ranking title="净流出前 10" rows={sortFundFlow(outflows, 'net_amount', true).slice(0, 10)} positive={false} />
                </div>}
                <section className="overflow-hidden rounded-card border border-border bg-surface">
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
                    <h2 className="text-sm font-medium text-foreground">{LABELS[type]}资金明细 <span className="ml-2 text-xs text-muted">{rows.length} 条</span></h2>
                    <div className="flex items-center gap-2">
                      <select aria-label="排序字段" className={INPUT} value={field} onChange={e => setSort(e.target.value as SortField)}>
                        {FIELDS.filter(f => caps?.amount_fields.includes(f.id)).map(f => <option key={f.id} value={f.id}>{f.label}</option>)}
                      </select>
                      <button aria-label="切换排序方向" className={INPUT} onClick={() => setAscending(v => !v)}>{ascending ? '从小到大 ↑' : '从大到小 ↓'}</button>
                    </div>
                  </div>
                  <div className="max-h-[580px] overflow-auto">
                    <table className="w-full min-w-[730px] text-left text-xs">
                      <thead className="sticky top-0 bg-elevated text-muted"><tr>
                        {['排名', LABELS[type], '涨跌幅', '流入（元）', '流出（元）', '净额（元）', '成员数'].map(h => <th className="whitespace-nowrap px-4 py-3 font-medium" key={h}>{h}</th>)}
                      </tr></thead>
                      <tbody className="divide-y divide-border/60">
                        {rows.map((r, index) => <tr key={r.source_subject_key} className="hover:bg-elevated/40">
                          <td className="px-4 py-3 font-mono text-muted">{index + 1}</td>
                          <td className="px-4 py-3"><div className="font-medium text-foreground">{r.name}</div><div className="mt-1 text-[10px] text-muted">{r.subject_id ?? '未关联板块代码'}</div></td>
                          <td className={cn('px-4 py-3 font-mono tabular-nums', priceColorClass(r.change_pct))}>{fmtPct(r.change_pct)}</td>
                          <td className="px-4 py-3 font-mono tabular-nums text-secondary">{fmtVolume(r.inflow_amount)}</td>
                          <td className="px-4 py-3 font-mono tabular-nums text-secondary">{fmtVolume(r.outflow_amount)}</td>
                          <td className={cn('px-4 py-3 font-mono tabular-nums', priceColorClass(r.net_amount))}>{fmtVolume(r.net_amount)}</td>
                          <td className="px-4 py-3 font-mono text-muted">{r.member_count ?? '—'}</td>
                        </tr>)}
                      </tbody>
                    </table>
                    {!rows.length && <p className="p-8 text-center text-sm text-muted">没有匹配的名称或代码</p>}
                  </div>
                </section>
              </>}
              <p className="text-[11px] leading-6 text-muted">金额为人民币；缺失值显示“—”。统计窗口与资金口径沿用当前数据源，抓取时间不代表成交更新时间。概念可能重叠，板块金额不能相加作为全市场资金；净流出与其他板块净流入不代表同一笔资金转移。</p>
            </>}
        </div>
      </div>
    </div>
  )
}

function State({ title, detail, action }: { title: string; detail?: string; action?: React.ReactNode }) {
  return <div className="rounded-card border border-border bg-surface"><EmptyState icon={Landmark} title={title} hint={detail} />{action && <div className="pb-8 text-center text-xs">{action}</div>}</div>
}

function Metric({ label, value, color = 'text-foreground' }: { label: string; value: string; color?: string }) {
  return <div className="min-w-0 rounded-card border border-border bg-surface px-4 py-4"><div className="text-xs text-muted">{label}</div><div className={cn('mt-2 truncate text-lg font-semibold tabular-nums', color)} title={value}>{value}</div></div>
}

function Ranking({ title, rows, positive }: { title: string; rows: FundFlowRow[]; positive: boolean }) {
  const max = Math.max(...rows.map(r => Math.abs(r.net_amount ?? 0)), 1)
  const Icon = positive ? ArrowUpRight : ArrowDownLeft
  return <section className="rounded-card border border-border bg-surface p-4">
    <h2 className="mb-4 flex items-center gap-2 text-sm font-medium text-foreground"><Icon className={cn('h-4 w-4', positive ? 'text-bull' : 'text-bear')} />{title}</h2>
    <div className="space-y-3" aria-label={title}>
      {rows.map((row, index) => <div key={row.source_subject_key} className="flex items-center gap-3 text-xs">
        <span className="w-4 shrink-0 font-mono text-muted">{index + 1}</span>
        <span className="w-24 shrink-0 truncate text-secondary sm:w-32" title={row.name}>{row.name}</span>
        <div className="h-2 min-w-0 flex-1 rounded bg-elevated"><div className={cn('h-full rounded', positive ? 'bg-bull/70' : 'bg-bear/70')} style={{ width: `${Math.max(1, Math.abs(row.net_amount ?? 0) / max * 100)}%` }} /></div>
        <span className={cn('w-24 shrink-0 text-right font-mono tabular-nums', priceColorClass(row.net_amount))}>{fmtVolume(row.net_amount)}</span>
      </div>)}
      {!rows.length && <p className="py-8 text-center text-xs text-muted">当前窗口暂无{positive ? '净流入' : '净流出'}记录</p>}
    </div>
  </section>
}

const extension: FrontendExtension = {
  id: 'official.fund-flow', apiVersion: 1,
  routes: [{ id: 'fund-flow', path: '/fund-flow', component: FundFlowPage }],
  navigation: [{ id: 'fund-flow', routeId: 'fund-flow', label: '资金流向', icon: Landmark, order: 510 }],
}
export default extension
