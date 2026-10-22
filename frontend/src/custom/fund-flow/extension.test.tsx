// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { CapabilityRoute, FundFlowQuery, FundFlowResponse, FundFlowRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fundFlowDay, fundFlowPolling } from '@/lib/useFundFlow'
import { FundFlowPage, sortFundFlow } from './extension'

const fixtures = vi.hoisted(() => ({
  route: undefined as CapabilityRoute | undefined,
  pending: [] as { query: FundFlowQuery; resolve: (value: FundFlowResponse) => void }[],
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilityMatrix: () => ({ data: { capabilities: fixtures.route ? [fixtures.route] : [] }, isLoading: false, isError: false }),
  useQuoteStatus: () => ({ data: { is_trading_hours: false } }),
}))
vi.mock('@/lib/api', () => ({ api: {
  fundFlow: (query: FundFlowQuery) => new Promise<FundFlowResponse>(resolve => fixtures.pending.push({ query, resolve })),
} }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const row = (name: string, net: number | null): FundFlowRow => ({
  name, net_amount: net, source_subject_key: name, subject_id: null,
  inflow_amount: null, outflow_amount: null, change_pct: null, member_count: null,
})

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.pending.length = 0
  fixtures.route = {
    id: 'fund_flow', effective: 'test', effective_display: '测试来源', usable: true,
    label: '资金流向', desc: '测试资金流', field: 'fund_flow_data_provider', default: 'test',
    tf_tier: null, tf_available: false, current: 'test', current_display: '测试来源',
    candidates: [], pending: [], details: {
      api_version: 1, taxonomy: 'test', metric_basis: 'public', metric_label: '公开资金',
      subject_types: ['industry', 'concept'], amount_fields: ['net_amount'],
      granularities: ['snapshot'], refresh_interval_s: 60,
      windows: [{ id: 'today', label: '当天', basis: 'source_defined', start: null, end: null },
        { id: '5d', label: '近5日', basis: 'source_defined', start: null, end: null }],
    },
  }
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })

async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render() {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><FundFlowPage /></QueryClientProvider></MemoryRouter>))
  await settle()
}
async function select(label: string, value: string) {
  const element = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!
  await act(async () => { element.value = value; element.dispatchEvent(new Event('change', { bubbles: true })) })
  await settle()
}
async function resolve(index: number, name: string, status: 'ok' | 'stale' = 'ok') {
  const pending = fixtures.pending[index]
  await act(async () => pending.resolve({
    api_version: 1, status, source: fixtures.route!.effective, query: pending.query,
    capabilities: fixtures.route!.details!, window: fixtures.route!.details!.windows[0],
    complete: true, currency: 'CNY', fetched_at: '2026-10-22T10:00:00+08:00',
    source_updated_at: null, amount_precision: 0.01, rows: [row(name, -100)],
    message: status === 'stale' ? '保留上次完整数据' : null,
  }))
  await settle()
}

it('does not fetch an unavailable optional source', async () => {
  fixtures.route!.usable = false
  await render()
  expect(host.textContent).toContain('资金流数据源未就绪')
  expect(fixtures.pending).toHaveLength(0)
})

it('isolates delayed responses across type, window and source switches', async () => {
  await render()
  expect(host.textContent).toContain('正在获取完整资金流榜单')
  await select('标的类型', 'concept')
  await resolve(0, '旧行业')
  expect(host.textContent).not.toContain('旧行业')
  await resolve(1, '新概念')
  expect(host.textContent).toContain('新概念')
  expect(host.textContent).toContain('来源未披露')
  expect(host.querySelectorAll('select[aria-label="排序字段"] option')).toHaveLength(1)
  await select('统计窗口', '5d')
  expect(host.textContent).not.toContain('新概念')
  await resolve(2, '多日概念')
  expect(host.textContent).toContain('多日概念')
  fixtures.route = { ...fixtures.route!, effective: 'second', details: { ...fixtures.route!.details!, taxonomy: 'second.taxonomy' } }
  await render()
  expect(host.textContent).not.toContain('多日概念')
  await resolve(3, '其他来源')
  expect(host.textContent).toContain('其他来源')
})

it('keeps the same-context snapshot visible while refreshing and labels stale data', async () => {
  await render()
  await resolve(0, '银行')
  const button = [...host.querySelectorAll('button')].find(b => b.textContent === '刷新')!
  await act(async () => button.click())
  await settle()
  expect(host.textContent).toContain('银行')
  await resolve(1, '银行', 'stale')
  expect(host.textContent).toContain('数据已过期')
  expect(host.textContent).toContain('银行')
})

it('keeps unknown amounts last and never treats them as zero', () => {
  expect(sortFundFlow([row('missing', null), row('out', -1), row('zero', 0)], 'net_amount', true).map(r => r.name)).toEqual(['out', 'zero', 'missing'])
})

it('rejects a response from a newly routed source before its matrix arrives', async () => {
  await render()
  const pending = fixtures.pending[0]
  await act(async () => pending.resolve({
    api_version: 1, status: 'ok', source: 'changed-while-loading', query: pending.query,
    capabilities: fixtures.route!.details!, window: fixtures.route!.details!.windows[0],
    complete: true, currency: 'CNY', fetched_at: null, source_updated_at: null,
    amount_precision: 0.01, rows: [row('错误来源数据', 9)], message: null,
  }))
  await settle()
  expect(host.textContent).not.toContain('错误来源数据')
  expect(host.textContent).toContain('来源或统计口径已变化')
})

it('does not present unavailable net measurements as zero counts', async () => {
  fixtures.route!.details!.amount_fields = ['inflow_amount']
  await render()
  await resolve(0, '只有流入数据')
  expect(host.textContent).not.toContain('净流入前 10')
  expect(host.textContent).toContain('只有流入数据')
})

it('polls only in visible Beijing trading sessions, including midnight/date isolation', () => {
  const morning = new Date('2026-10-22T02:00:00Z')
  expect(fundFlowPolling(morning, true, true)).toBe(60_000)
  expect(fundFlowPolling(morning, false, true)).toBe(false)
  expect(fundFlowPolling(morning, true, false)).toBe(false)
  expect(fundFlowPolling(new Date('2026-10-22T04:00:00Z'), true, true)).toBe(false)
  expect(fundFlowPolling(new Date('2026-10-24T02:00:00Z'), true, undefined)).toBe(false)
  expect(fundFlowDay(new Date('2026-10-22T16:01:00Z'))).toBe('2026-10-23')
  expect(QK.fundFlow('a', 'ths', 'public', 'industry', 'today', '2026-10-22')).not.toEqual(QK.fundFlow('a', 'ths', 'main', 'industry', 'today', '2026-10-22'))
})
