// src/components/ThinkingToggle.tsx
// Local-model reasoning toggle — only meaningful for the local (ollama / nemotron) model in
// normal chat. ON (default) = the model reasons before answering (slower, can time out on
// the heavy in-play queries). OFF = native Ollama think:false → fast, no reasoning overhead.

import { Brain, Zap } from 'lucide-react'

interface Props {
  enabled: boolean   // thinking ON
  onChange: (v: boolean) => void
  disabled?: boolean
}

export function ThinkingToggle({ enabled, onChange, disabled }: Props) {
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange(!enabled)}
      disabled={disabled}
      title={enabled
        ? 'Thinking ON — the local model reasons first (slower, richer). Click for Fast mode.'
        : 'Fast mode — reasoning OFF (quicker, avoids timeouts). Click to enable Thinking.'}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 5,
        padding: '3px 10px',
        border: '2px solid var(--ink)',
        background: enabled ? 'var(--ink)' : 'var(--paper)',
        color: enabled ? 'var(--paper)' : 'var(--ink)',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        fontFamily: 'var(--font-mono)',
        fontSize: '10px',
        fontWeight: 700,
        letterSpacing: '.06em',
        textTransform: 'uppercase' as const,
        transition: 'all .15s',
        boxShadow: enabled ? 'none' : '2px 2px 0 0 var(--ink)',
      }}
    >
      {enabled ? <Brain style={{ width: 11, height: 11 }} /> : <Zap style={{ width: 11, height: 11, fill: 'currentColor' }} />}
      <span>{enabled ? 'Thinking' : 'Fast'}</span>
    </button>
  )
}
