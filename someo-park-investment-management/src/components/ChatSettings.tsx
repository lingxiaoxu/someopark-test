import React from 'react'
import { LLMModelConfig } from '../lib/models'
import { Settings2, ExternalLink } from 'lucide-react'
import { useState, useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'

export function ChatSettings({
  apiKeyConfigurable,
  baseURLConfigurable,
  languageModel,
  onLanguageModelChange,
  useMorphApply,
  onUseMorphApplyChange,
}: {
  apiKeyConfigurable: boolean
  baseURLConfigurable: boolean
  languageModel: LLMModelConfig
  onLanguageModelChange: (model: LLMModelConfig) => void
  useMorphApply: boolean
  onUseMorphApplyChange: (enabled: boolean) => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const inputStyle: React.CSSProperties = {
    width: '100%', padding: '5px 8px',
    background: 'var(--bg-tertiary)', border: '2px solid var(--ink)',
    fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--ink)',
    outline: 'none',
  }
  const numInputStyle: React.CSSProperties = {
    width: 72, height: 26, padding: '0 6px',
    background: 'var(--bg-tertiary)', border: '2px solid var(--hairline)',
    fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--ink)',
    textAlign: 'center', outline: 'none',
  }
  const labelStyle: React.CSSProperties = {
    fontSize: '10px', fontWeight: 700, letterSpacing: '.1em',
    textTransform: 'uppercase', color: 'var(--ink-mute)',
    fontFamily: 'var(--font-mono)', display: 'block', marginBottom: 4,
  }
  const dividerStyle: React.CSSProperties = { borderTop: '1px solid var(--page)', margin: '8px 0' }

  return (
    <div style={{ position: 'relative' }} ref={ref}>
      {/* Trigger — Stanse-style border-2 button */}
      <button
        type="button"
        onClick={() => setOpen(!open)}
        title={t('chatSettings.title')}
        style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          padding: '3px 8px',
          background: open ? 'var(--ink)' : 'var(--paper)',
          color: open ? 'var(--paper)' : 'var(--ink)',
          border: '2px solid var(--ink)',
          boxShadow: open ? 'none' : '2px 2px 0 0 var(--ink)',
          fontFamily: 'var(--font-mono)', cursor: 'pointer', transition: 'all .1s',
          transform: open ? 'translate(2px,2px)' : 'none',
        }}
        onMouseEnter={e => { if (!open) { (e.currentTarget as HTMLElement).style.background = 'var(--ink)'; (e.currentTarget as HTMLElement).style.color = 'var(--paper)' } }}
        onMouseLeave={e => { if (!open) { (e.currentTarget as HTMLElement).style.background = 'var(--paper)'; (e.currentTarget as HTMLElement).style.color = 'var(--ink)' } }}
      >
        <Settings2 style={{ width: 14, height: 14 }} />
      </button>

      {open && (
        <div style={{
          position: 'absolute', bottom: 'calc(100% + 6px)', right: 0,
          width: 260, background: 'var(--paper)',
          border: '2px solid var(--ink)', boxShadow: '4px 4px 0 0 var(--ink)',
          zIndex: 100, padding: '12px',
        }}>

          {/* Header */}
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--ink)', borderBottom: '2px solid var(--ink)', paddingBottom: 8, marginBottom: 12 }}>
            {t('chatSettings.title')}
          </div>

          {/* Morph Apply — Stanse-style toggle (checkbox-like) */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--ink)' }}>{t('chatSettings.morphApply')}</span>
            <button
              type="button"
              onClick={() => onUseMorphApplyChange(!useMorphApply)}
              style={{
                width: 44, height: 22,
                background: useMorphApply ? 'var(--ink)' : 'var(--page)',
                border: '2px solid var(--ink)',
                cursor: 'pointer', position: 'relative', transition: 'background .15s',
                padding: 0,
              }}
            >
              <span style={{
                position: 'absolute', top: 1,
                left: useMorphApply ? 'calc(100% - 20px)' : 2,
                width: 16, height: 16,
                background: useMorphApply ? 'var(--paper)' : 'var(--ink-mute)',
                border: '1px solid var(--ink)',
                transition: 'left .15s, background .15s',
                display: 'block',
              }} />
            </button>
          </div>
          <a style={{ fontSize: '11px', color: 'var(--ink-mute)', fontFamily: 'var(--font-mono)', display: 'flex', alignItems: 'center', gap: 4, textDecoration: 'none' }}
            target="_blank" href="https://morphllm.com" rel="noreferrer"
            onMouseEnter={e => (e.currentTarget as HTMLElement).style.color = 'var(--ink)'}
            onMouseLeave={e => (e.currentTarget as HTMLElement).style.color = 'var(--ink-mute)'}
          >
            {t('chatSettings.learnMorph')} <ExternalLink style={{ width: 11, height: 11 }} />
          </a>

          <div style={dividerStyle} />

          {/* API Key */}
          {apiKeyConfigurable && (
            <>
              <div style={{ marginBottom: 8 }}>
                <label style={labelStyle}>{t('chatSettings.apiKey')}</label>
                <input type="password" placeholder="Auto" defaultValue={languageModel.apiKey}
                  onChange={e => onLanguageModelChange({ apiKey: e.target.value || undefined })}
                  style={inputStyle}
                  onFocus={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--ink)'}
                />
              </div>
              <div style={dividerStyle} />
            </>
          )}

          {/* Base URL */}
          {baseURLConfigurable && (
            <>
              <div style={{ marginBottom: 8 }}>
                <label style={labelStyle}>{t('chatSettings.baseURL')}</label>
                <input type="text" placeholder="Auto" defaultValue={languageModel.baseURL}
                  onChange={e => onLanguageModelChange({ baseURL: e.target.value || undefined })}
                  style={inputStyle}
                />
              </div>
              <div style={dividerStyle} />
            </>
          )}

          {/* Parameters */}
          <div>
            <div style={{ ...labelStyle, marginBottom: 10 }}>{t('chatSettings.parameters')}</div>
            {[
              { label: t('chatSettings.outputTokens'), key: 'maxTokens', min: 50, max: 10000, step: 1 },
              { label: t('chatSettings.temperature'), key: 'temperature', min: 0, max: 5, step: 0.01 },
              { label: 'Top P', key: 'topP', min: 0, max: 1, step: 0.01 },
              { label: 'Top K', key: 'topK', min: 0, max: 500, step: 1 },
              { label: t('chatSettings.frequencyPenalty'), key: 'frequencyPenalty', min: 0, max: 2, step: 0.01 },
              { label: t('chatSettings.presencePenalty'), key: 'presencePenalty', min: 0, max: 2, step: 0.01 },
            ].map(({ label, key, min, max, step }) => (
              <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span style={{ flex: 1, fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--ink-dim)' }}>{label}</span>
                <input
                  type="number"
                  defaultValue={(languageModel as any)[key]}
                  min={min} max={max} step={step}
                  placeholder="Auto"
                  onChange={e => onLanguageModelChange({ [key]: parseFloat(e.target.value) || undefined })}
                  style={numInputStyle}
                  onFocus={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--ink)'}
                  onBlur={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--hairline)'}
                />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
