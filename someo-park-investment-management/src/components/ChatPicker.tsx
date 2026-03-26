import { LLMModelConfig } from '../lib/models'
import { Templates } from '../lib/templates'
import { Sparkles, ChevronDown } from 'lucide-react'
import React, { useState, useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'

export type LLMModel = {
  id: string
  name: string
  provider: string
  providerId: string
  multiModal?: boolean
}

function Dropdown({ trigger, children, open, setOpen }: { trigger: React.ReactNode, children: React.ReactNode, open: boolean, setOpen: (o: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [setOpen])

  return (
    <div style={{ position: 'relative' }} ref={ref}>
      <div onClick={() => setOpen(!open)}>{trigger}</div>
      {open && (
        <div style={{
          position: 'absolute',
          bottom: 'calc(100% + 4px)',
          left: 0,
          minWidth: 200,
          maxHeight: 300,
          overflowY: 'auto',
          background: '#fff',
          border: '2px solid #111',
          boxShadow: '4px 4px 0 0 #111',
          zIndex: 100,
        }}>
          {children}
        </div>
      )}
    </div>
  )
}

export function ChatPicker({
  templates,
  selectedTemplate,
  onSelectedTemplateChange,
  models,
  languageModel,
  onLanguageModelChange,
}: {
  templates: Templates
  selectedTemplate: string
  onSelectedTemplateChange: (template: string) => void
  models: LLMModel[]
  languageModel: LLMModelConfig
  onLanguageModelChange: (config: LLMModelConfig) => void
}) {
  const { t } = useTranslation()
  const [templateOpen, setTemplateOpen] = useState(false)
  const [modelOpen, setModelOpen] = useState(false)

  const selectedModelName = models.find(m => m.id === languageModel.model)?.name || t('chatPicker.selectModel')

  // Group models by provider
  const grouped = models.reduce((acc, m) => {
    if (!acc[m.provider]) acc[m.provider] = []
    acc[m.provider].push(m)
    return acc
  }, {} as Record<string, LLMModel[]>)

  // Shared styles
  const triggerStyle: React.CSSProperties = {
    display: 'inline-flex', alignItems: 'center', gap: 4,
    padding: '3px 8px',
    background: '#fff', color: '#111',
    border: '2px solid #111',
    boxShadow: '2px 2px 0 0 #111',
    fontFamily: 'var(--font-mono)', fontSize: '11px', fontWeight: 700,
    letterSpacing: '.04em', textTransform: 'uppercase',
    cursor: 'pointer', transition: 'all .1s',
  }
  const menuItemStyle = (active: boolean): React.CSSProperties => ({
    display: 'flex', alignItems: 'center', gap: 8,
    width: '100%', padding: '6px 12px',
    background: active ? '#111' : 'transparent',
    color: active ? '#fff' : '#333',
    border: 'none', borderBottom: '1px solid #e5e5e5',
    fontFamily: 'var(--font-mono)', fontSize: '12px',
    cursor: 'pointer', textAlign: 'left', transition: 'background .1s',
  })
  const groupLabelStyle: React.CSSProperties = {
    padding: '6px 12px 3px',
    fontSize: '9px', fontWeight: 700,
    letterSpacing: '.14em', textTransform: 'uppercase',
    color: '#888', fontFamily: 'var(--font-mono)',
    borderBottom: '1px solid #e5e5e5',
    background: '#f4f4f4',
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      {/* Template picker */}
      <Dropdown open={templateOpen} setOpen={setTemplateOpen} trigger={
        <button type="button" style={triggerStyle}
          onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = '#111'; (e.currentTarget as HTMLElement).style.color = '#fff' }}
          onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = '#fff'; (e.currentTarget as HTMLElement).style.color = '#111' }}
        >
          <Sparkles style={{ width: 11, height: 11 }} />
          <span>{selectedTemplate === 'auto' ? 'Auto' : (templates as any)[selectedTemplate]?.name || selectedTemplate}</span>
          <ChevronDown style={{ width: 11, height: 11 }} />
        </button>
      }>
        <div>
          <div style={groupLabelStyle}>{t('chatPicker.persona')}</div>
          <button onClick={() => { onSelectedTemplateChange('auto'); setTemplateOpen(false) }} style={menuItemStyle(selectedTemplate === 'auto')}
            onMouseEnter={e => { if (selectedTemplate !== 'auto') (e.currentTarget as HTMLElement).style.background = '#f4f4f4' }}
            onMouseLeave={e => { if (selectedTemplate !== 'auto') (e.currentTarget as HTMLElement).style.background = 'transparent' }}
          >
            <Sparkles style={{ width: 12, height: 12 }} /> Auto
          </button>
          {Object.entries(templates).map(([id, tmpl]) => (
            <button key={id} onClick={() => { onSelectedTemplateChange(id); setTemplateOpen(false) }} style={menuItemStyle(selectedTemplate === id)}
              onMouseEnter={e => { if (selectedTemplate !== id) (e.currentTarget as HTMLElement).style.background = '#f4f4f4' }}
              onMouseLeave={e => { if (selectedTemplate !== id) (e.currentTarget as HTMLElement).style.background = 'transparent' }}
            >
              {(tmpl as any).name}
            </button>
          ))}
        </div>
      </Dropdown>

      {/* Model picker */}
      <Dropdown open={modelOpen} setOpen={setModelOpen} trigger={
        <button type="button" style={triggerStyle}
          onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = '#111'; (e.currentTarget as HTMLElement).style.color = '#fff' }}
          onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = '#fff'; (e.currentTarget as HTMLElement).style.color = '#111' }}
        >
          <span>{selectedModelName}</span>
          <ChevronDown style={{ width: 11, height: 11 }} />
        </button>
      }>
        <div>
          {Object.entries(grouped).map(([provider, providerModels]) => (
            <div key={provider}>
              <div style={groupLabelStyle}>{provider}</div>
              {providerModels.map(m => (
                <button key={m.id} onClick={() => { onLanguageModelChange({ model: m.id }); setModelOpen(false) }} style={menuItemStyle(languageModel.model === m.id)}
                  onMouseEnter={e => { if (languageModel.model !== m.id) (e.currentTarget as HTMLElement).style.background = '#f4f4f4' }}
                  onMouseLeave={e => { if (languageModel.model !== m.id) (e.currentTarget as HTMLElement).style.background = 'transparent' }}
                >
                  {m.name}
                </button>
              ))}
            </div>
          ))}
        </div>
      </Dropdown>
    </div>
  )
}
