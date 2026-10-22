export const INTRADAY_DAY_OPTIONS = [1, 5, 10, 20] as const

/** 浅源只能选择其覆盖范围内的档位，默认值也必须是可选项。 */
export function intradayDayChoices(historyDays: number | null) {
  const options = INTRADAY_DAY_OPTIONS.filter(d => historyDays == null || d <= historyDays)
  const defaultDays = historyDays != null && historyDays < 20
    ? [...options].reverse().find(d => d <= 5) ?? options[0] ?? 1
    : 20
  return { options, defaultDays }
}
