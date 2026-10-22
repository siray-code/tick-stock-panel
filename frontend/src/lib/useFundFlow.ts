import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type CapabilityRoute, type FundFlowQuery } from './api'
import { QK } from './queryKeys'
import { useQuoteStatus } from './useSharedQueries'

export function fundFlowDay(now = new Date()) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(now)
}

export function fundFlowPolling(now: Date, visible: boolean, trading: boolean | undefined, seconds = 60): number | false {
  if (!visible || trading === false) return false
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(now).map(p => [p.type, p.value]))
  if (parts.weekday === 'Sat' || parts.weekday === 'Sun') return false
  const time = `${parts.hour}:${parts.minute}`
  return ((time >= '09:30' && time <= '11:30') || (time >= '13:00' && time <= '15:00'))
    ? Math.max(60, seconds) * 1000 : false
}

/** Shared by all funds consumers. Query keys isolate source, measurement and date. */
export function useFundFlow(query: FundFlowQuery, route: CapabilityRoute | undefined) {
  const client = useQueryClient()
  const [now, setNow] = useState(() => new Date())
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden')
  const quoteStatus = useQuoteStatus()
  useEffect(() => {
    const update = () => {
      const active = document.visibilityState !== 'hidden'
      setVisible(active)
      if (active) setNow(new Date())
    }
    const timer = window.setInterval(update, 30_000)
    document.addEventListener('visibilitychange', update)
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', update) }
  }, [])
  const caps = route?.details
  const interval = fundFlowPolling(now, visible, quoteStatus.data?.is_trading_hours, caps?.refresh_interval_s)
  return useQuery({
    queryKey: QK.fundFlow(route?.effective ?? '', caps?.taxonomy ?? '', caps?.metric_basis ?? '',
      query.subject_type, query.window_id, fundFlowDay(now), query.subject_ids, query.granularity),
    queryFn: async () => {
      const result = await api.fundFlow(query)
      // Routing can change while a request is in flight or before the matrix refreshes.
      if (result.source !== route?.effective || (result.capabilities && (
        result.capabilities.taxonomy !== caps?.taxonomy || result.capabilities.metric_basis !== caps?.metric_basis
      ))) {
        void client.invalidateQueries({ queryKey: QK.capabilityMatrix })
        throw new Error('资金流来源或统计口径已变化，正在重新读取能力配置')
      }
      const ids = (values?: string[]) => JSON.stringify([...new Set(values ?? [])].sort())
      if (result.api_version !== 1 || result.query.subject_type !== query.subject_type
        || result.query.window_id !== query.window_id || ids(result.query.subject_ids) !== ids(query.subject_ids)
        || (result.query.granularity ?? 'snapshot') !== (query.granularity ?? 'snapshot')) {
        throw new Error('资金流返回的契约版本或查询上下文不一致')
      }
      return result
    },
    enabled: Boolean(route?.usable && caps?.subject_types.includes(query.subject_type)
      && caps.windows.some(w => w.id === query.window_id) && caps.granularities.includes(query.granularity ?? 'snapshot')),
    staleTime: Math.max(60, caps?.refresh_interval_s ?? 60) * 1000,
    refetchInterval: interval,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
    retry: false,
  })
}
