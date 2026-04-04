// src/components/AgentProgress.tsx
// Renders agent steps: thinking, tool calls, task progress, ask_user, usage stats
// Reference: CC src/tools/BashTool, TodoWriteTool, AskUserQuestionTool rendering

import React, { useState } from 'react'
import type { AgentStep, TaskItem } from '../lib/messages'

const TOOL_DISPLAY: Record<string, string> = {
  get_inventory: '查询持仓',
  get_signals: '获取信号',
  get_regime: '查询市场状态',
  get_daily_report: '获取日报',
  get_daily_report_text: '获取日报文本',
  get_wf_summary: '获取 WF 汇总',
  get_equity_curve: '获取净值曲线',
  get_oos_pair_summary: '获取配对统计',
  get_dsr_log: '获取 DSR 日志',
  get_pair_universe: '查询配对宇宙',
  get_monitor_history: '查询 Monitor 历史',
  get_wf_diagnostic: '获取 WF 诊断',
  get_wf_structure: '获取 WF 结构',
  get_strategy_performance: '查询策略业绩',
  get_pnl_reports: '查询 PnL 报告',
  get_inventory_history: '查询历史持仓',
  query_mongodb: '查询 MongoDB',
  calculate: '数学计算',
  calculate_statistics: '统计计算',
  read_file: '读取文件',
  parse_data_file: '解析数据文件',
  list_files: '列出文件',
  query_json: 'JSON 查询',
  http_request: 'HTTP 请求',
  get_datetime: '获取时间',
  compare_strategies: '策略对比',
  get_pair_stats: '配对详细统计',
  ask_user: '向用户提问',
  manage_tasks: '更新任务清单',
}

// Icons as inline SVG for minimal dependencies
function CheckIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
}
function ErrorIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
}
function SpinnerIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#8b5cf6" strokeWidth="2.5" strokeLinecap="round" style={{ animation: 'spin 1s linear infinite' }}><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
}
function WrenchIcon() {
  return <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#999" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
}
function BrainIcon() {
  return <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#999" strokeWidth="2" strokeLinecap="round"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44"/></svg>
}
function ZapIcon() {
  return <svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" strokeWidth="1"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
}

function ToolCard({ call, result }: {
  call: Extract<AgentStep, { type: 'tool_call' }>
  result?: Extract<AgentStep, { type: 'tool_result' }>
}) {
  const [open, setOpen] = useState(false)
  const done = result !== undefined
  const err = result?.isError

  return (
    <div style={{
      border: `2px solid ${err ? '#fca5a5' : '#e5e5e5'}`,
      background: err ? '#fef2f2' : '#fafafa',
      fontSize: '12px',
      overflow: 'hidden',
    }}>
      <div
        onClick={(e) => { e.preventDefault(); setOpen(o => !o) }}
        style={{
          display: 'flex', alignItems: 'center', gap: 6,
          padding: '6px 8px', cursor: 'pointer',
          userSelect: 'none',
        }}
      >
        {done ? (err ? <ErrorIcon /> : <CheckIcon />) : <SpinnerIcon />}
        <WrenchIcon />
        <span style={{ fontWeight: 600, color: '#333', fontFamily: 'var(--font-mono)' }}>
          {TOOL_DISPLAY[call.toolName] || call.toolName}
        </span>
        <code style={{ color: '#999', fontFamily: 'var(--font-mono)', fontSize: '10px', marginLeft: 2 }}>{call.toolName}</code>
        <span style={{ marginLeft: 'auto', color: '#999', fontSize: '10px' }}>
          {open ? '▼' : '▶'}
        </span>
      </div>
      {open && (
        <div style={{ borderTop: '1px solid inherit', padding: '6px 10px', overflowAnchor: 'none' as any }}>
          {Object.keys(call.toolInput).length > 0 && (
            <div style={{ marginBottom: 6 }}>
              <div style={{ fontSize: '10px', color: '#999', textTransform: 'uppercase', letterSpacing: '.08em', marginBottom: 2 }}>Input</div>
              <pre style={{ fontSize: '11px', color: '#555', overflowX: 'auto', whiteSpace: 'pre-wrap', fontFamily: 'var(--font-mono)' }}>
                {JSON.stringify(call.toolInput, null, 2)}
              </pre>
            </div>
          )}
          {result && (
            <div>
              <div style={{ fontSize: '10px', color: err ? '#ef4444' : '#999', textTransform: 'uppercase', letterSpacing: '.08em', marginBottom: 2 }}>
                {err ? 'Error' : 'Result'}
              </div>
              <pre style={{
                fontSize: '11px', overflowX: 'auto', maxHeight: 160, whiteSpace: 'pre-wrap',
                fontFamily: 'var(--font-mono)',
                color: err ? '#dc2626' : '#555',
              }}>
                {result.toolResult.length > 600
                  ? result.toolResult.slice(0, 600) + `\n... (${result.toolResult.length - 600} chars omitted)`
                  : result.toolResult}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function TaskProgressCard({ tasks }: { tasks: TaskItem[] }) {
  const done = tasks.filter(t => t.status === 'completed').length
  const pct = tasks.length > 0 ? (done / tasks.length) * 100 : 0

  return (
    <div style={{ border: '2px solid #93c5fd', background: '#eff6ff', fontSize: '12px', padding: '8px 10px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#1d4ed8', fontWeight: 600, marginBottom: 6 }}>
        <span>📋</span>
        <span>任务进度 {done}/{tasks.length}</span>
        <div style={{ flex: 1, height: 4, background: '#bfdbfe', borderRadius: 2, overflow: 'hidden' }}>
          <div style={{ height: '100%', background: '#3b82f6', transition: 'width .3s', width: `${pct}%` }} />
        </div>
      </div>
      {tasks.map(t => (
        <div key={t.id} style={{ display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 4, marginBottom: 2 }}>
          {t.status === 'completed' ? <CheckIcon />
            : t.status === 'failed' ? <ErrorIcon />
              : t.status === 'in_progress' ? <SpinnerIcon />
                : <span style={{ width: 10, height: 10, borderRadius: '50%', border: '2px solid #999', display: 'inline-block' }} />}
          <span style={{
            textDecoration: t.status === 'completed' ? 'line-through' : 'none',
            color: t.status === 'completed' ? '#999' : '#333',
            fontFamily: 'var(--font-mono)',
          }}>
            {t.title}
          </span>
        </div>
      ))}
    </div>
  )
}

function AskUserCard({ question, options, onAnswer }: {
  question: string; options?: string[]; onAnswer: (answer: string) => void
}) {
  return (
    <div style={{ border: '2px solid #fbbf24', background: '#fffbeb', fontSize: '12px', padding: '8px 10px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#92400e', fontWeight: 600, marginBottom: 6 }}>
        <span>❓</span>
        <span>{question}</span>
      </div>
      {options && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, paddingLeft: 4 }}>
          {options.map(opt => (
            <button key={opt}
              onClick={() => onAnswer(opt)}
              style={{
                padding: '4px 12px', border: '2px solid #fbbf24', background: '#fef3c7',
                color: '#92400e', cursor: 'pointer', fontFamily: 'var(--font-mono)', fontSize: '11px',
                fontWeight: 600, transition: 'background .15s',
              }}
              onMouseEnter={e => { (e.target as HTMLElement).style.background = '#fde68a' }}
              onMouseLeave={e => { (e.target as HTMLElement).style.background = '#fef3c7' }}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function UsageBar({ usage }: { usage: Extract<AgentStep, { type: 'usage' }> }) {
  return (
    <div style={{
      display: 'flex', gap: 10, fontSize: '10px', color: '#999',
      fontFamily: 'var(--font-mono)', marginTop: 4, letterSpacing: '.02em',
    }}>
      <span>{usage.input_tokens.toLocaleString()} in</span>
      <span>{usage.output_tokens.toLocaleString()} out</span>
      {usage.cache_read_tokens > 0 && <span>{usage.cache_read_tokens.toLocaleString()} cache↑</span>}
      <span style={{ fontWeight: 600 }}>${usage.cost_usd.toFixed(4)}</span>
      <span>{usage.iterations} turns</span>
    </div>
  )
}

interface Props {
  steps: AgentStep[]
  isRunning: boolean
  onAskUserAnswer?: (answer: string) => void
  usage?: Extract<AgentStep, { type: 'usage' }> | null
}

export function AgentProgress({ steps, isRunning, onAskUserAnswer, usage }: Props) {
  const [collapsed, setCollapsed] = useState(false)

  if (steps.length === 0 && !isRunning) return null

  // Build rendered elements — deduplicate task_updates (only show latest)
  const rendered: React.ReactNode[] = []
  let callIdx = 0

  // Find the last task_update index so we only render that one
  let lastTaskIdx = -1
  for (let j = steps.length - 1; j >= 0; j--) {
    if (steps[j].type === 'task_update') { lastTaskIdx = j; break }
  }

  for (let i = 0; i < steps.length; i++) {
    const s = steps[i]
    if (s.type === 'thinking') {
      rendered.push(
        <div key={`t${i}`} style={{
          fontSize: '12px', color: '#999', fontStyle: 'italic',
          paddingLeft: 20, borderLeft: '2px solid #e5e5e5',
          fontFamily: 'var(--font-mono)',
        }}>
          <BrainIcon /> {s.text.length > 200 ? s.text.slice(0, 200) + '…' : s.text}
        </div>
      )
    } else if (s.type === 'tool_call') {
      const key = `c${callIdx++}`
      const callId = (s as any).toolUseId
      // Match tool_result by toolUseId (CC pattern — exact match, no ambiguity)
      const result = steps.find(
        r => r.type === 'tool_result' && (r as any).toolUseId === callId
      ) as Extract<AgentStep, { type: 'tool_result' }> | undefined
      rendered.push(<div key={key}><ToolCard call={s} result={result} /></div>)
    } else if (s.type === 'task_update') {
      // Only render the LAST task_update
      if (i === lastTaskIdx) {
        rendered.push(<div key={`tu${i}`}><TaskProgressCard tasks={s.tasks} /></div>)
      }
    } else if (s.type === 'ask_user') {
      rendered.push(
        <div key={`au${i}`}><AskUserCard question={s.question} options={s.options}
          onAnswer={onAskUserAnswer || (() => { })} /></div>
      )
    }
  }

  // Count completed tools for summary
  const toolCalls = steps.filter(s => s.type === 'tool_call')
  const completedTools = toolCalls.filter(s => (s as any).status === 'completed' || (s as any).status === 'error')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, margin: '6px 0' }}>
      {/* Header — clickable to toggle collapse */}
      <div
        onClick={() => !isRunning && setCollapsed(c => !c)}
        style={{
          display: 'flex', alignItems: 'center', gap: 6, fontSize: '11px', color: '#8b5cf6',
          cursor: isRunning ? 'default' : 'pointer', userSelect: 'none',
        }}
      >
        <div style={{
          width: 16, height: 16, background: '#ede9fe', display: 'flex',
          alignItems: 'center', justifyContent: 'center',
        }}>
          <ZapIcon />
        </div>
        <span style={{ fontWeight: 600, fontFamily: 'var(--font-mono)', letterSpacing: '.04em', textTransform: 'uppercase' }}>
          Someo Agent
        </span>
        {isRunning && <SpinnerIcon />}
        {!isRunning && toolCalls.length > 0 && (
          <span style={{ color: '#999', fontFamily: 'var(--font-mono)', fontSize: '10px' }}>
            {completedTools.length} tool{completedTools.length !== 1 ? 's' : ''} used
          </span>
        )}
        {!isRunning && (
          <span style={{ color: '#bbb', fontSize: '10px', marginLeft: 'auto' }}>
            {collapsed ? '▶ show' : '▼ hide'}
          </span>
        )}
      </div>

      {/* Collapsible body */}
      {!collapsed && (
        <div style={{ paddingLeft: 22, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {rendered}
        </div>
      )}

      {/* Usage always visible */}
      {usage && <div style={{ paddingLeft: 22 }}><UsageBar usage={usage} /></div>}
    </div>
  )
}
