// src/components/AgentModeToggle.tsx
// Someo Agent mode toggle button — Stanse pixel art style

import { Zap, MessageSquare } from 'lucide-react'

interface Props {
  enabled: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}

export function AgentModeToggle({ enabled, onChange, disabled }: Props) {
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange(!enabled)}
      disabled={disabled}
      title={enabled ? 'Someo Agent ON — click to switch to normal mode' : 'Click to enable Someo Agent'}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 5,
        padding: '3px 10px',
        border: '2px solid #111',
        background: enabled ? '#111' : '#fff',
        color: enabled ? '#fff' : '#555',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        fontFamily: 'var(--font-mono)',
        fontSize: '10px',
        fontWeight: 700,
        letterSpacing: '.06em',
        textTransform: 'uppercase' as const,
        transition: 'all .15s',
        boxShadow: enabled ? 'none' : '2px 2px 0 0 #111',
      }}
    >
      {enabled ? <Zap style={{ width: 11, height: 11, fill: 'currentColor' }} /> : <MessageSquare style={{ width: 11, height: 11 }} />}
      <span>{enabled ? 'Someo Agent' : 'Try Agent ⚡'}</span>
      {enabled && (
        <span style={{
          width: 6, height: 6, borderRadius: '50%',
          background: '#00cc66',
          animation: 'pulse 2s infinite',
          flexShrink: 0,
        }} />
      )}
    </button>
  )
}
