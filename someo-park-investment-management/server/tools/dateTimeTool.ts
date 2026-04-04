// server/tools/dateTimeTool.ts
// Reference: CC src/tools/BashTool (date command pattern)

import type { AgentTool } from './index.js'

// Simple NYSE market calendar (no external dependency)
function isWeekend(d: Date): boolean {
  return d.getDay() === 0 || d.getDay() === 6
}

// Major US market holidays (approximate)
const HOLIDAYS_2026 = new Set([
  '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03',
  '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07',
  '2026-11-26', '2026-12-25',
])

function isMarketDay(d: Date): boolean {
  if (isWeekend(d)) return false
  const ds = d.toLocaleDateString('en-CA')
  return !HOLIDAYS_2026.has(ds)
}

function findNextMarketDay(d: Date): string {
  const next = new Date(d)
  do { next.setDate(next.getDate() + 1) } while (!isMarketDay(next))
  return next.toLocaleDateString('en-CA')
}

function findPrevMarketDay(d: Date): string {
  const prev = new Date(d)
  do { prev.setDate(prev.getDate() - 1) } while (!isMarketDay(prev))
  return prev.toLocaleDateString('en-CA')
}

export const dateTimeTool: AgentTool = {
  definition: {
    name: 'get_datetime',
    description: 'Get current date/time info including NYSE market calendar.',
    input_schema: {
      type: 'object',
      properties: {
        timezone: { type: 'string', description: 'Timezone (default: America/New_York)' }
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ timezone = 'America/New_York' }) {
    const now = new Date()
    const formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      weekday: 'long', hour12: false,
    })
    const parts = formatter.formatToParts(now)
    const get = (t: string) => parts.find(p => p.type === t)?.value || ''

    const dateStr = `${get('year')}-${get('month')}-${get('day')}`
    const today = new Date(dateStr)

    return {
      date: dateStr,
      time: `${get('hour')}:${get('minute')}:${get('second')}`,
      weekday: get('weekday'),
      timezone,
      is_market_day: isMarketDay(today),
      next_market_day: findNextMarketDay(today),
      prev_market_day: findPrevMarketDay(today),
      utc: now.toISOString(),
    }
  }
}
