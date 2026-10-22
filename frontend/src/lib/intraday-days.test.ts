import { describe, expect, it } from 'vitest'
import { intradayDayChoices } from './intraday-days'

describe('minute provider history depth', () => {
  it('uses a valid default for a four-day source', () => {
    expect(intradayDayChoices(4)).toEqual({ options: [1], defaultDays: 1 })
  })
  it('preserves existing shallow five-day behavior', () => {
    expect(intradayDayChoices(5)).toEqual({ options: [1, 5], defaultDays: 5 })
  })
  it('keeps the full options for deep sources', () => {
    expect(intradayDayChoices(null)).toEqual({ options: [1, 5, 10, 20], defaultDays: 20 })
    expect(intradayDayChoices(40)).toEqual({ options: [1, 5, 10, 20], defaultDays: 20 })
  })
})
