import { StanseAgentSchema } from '../lib/schema'
import { ExecutionResult, ExecutionResultWeb } from '../lib/types'
import { DeepPartial } from 'ai'
import { ChevronsRight, LoaderCircle, Copy, Check, Rocket, Clock } from 'lucide-react'
import React, { useState, useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'

type Duration = '30m' | '1h' | '3h' | '6h' | '1d'

const DURATION_OPTIONS: { value: Duration; labelKey: string }[] = [
  { value: '30m', labelKey: 'codePreview.dur30m' },
  { value: '1h', labelKey: 'codePreview.dur1h' },
  { value: '3h', labelKey: 'codePreview.dur3h' },
  { value: '6h', labelKey: 'codePreview.dur6h' },
  { value: '1d', labelKey: 'codePreview.dur1d' },
]

export function CodePreview({
  stanseAgent,
  result,
  isChatLoading,
  isPreviewLoading,
  onClose,
}: {
  stanseAgent?: DeepPartial<StanseAgentSchema>
  result?: ExecutionResult
  isChatLoading: boolean
  isPreviewLoading: boolean
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [selectedTab, setSelectedTab] = useState<'code' | 'preview'>('code')
  const [copied, setCopied] = useState(false)
  const [deployOpen, setDeployOpen] = useState(false)
  const [selectedDuration, setSelectedDuration] = useState<Duration | null>(null)
  const [isDeploying, setIsDeploying] = useState(false)
  const [deployed, setDeployed] = useState(false)
  const [deployedUrl, setDeployedUrl] = useState<string | null>(null)
  const [urlCopied, setUrlCopied] = useState(false)
  const deployRef = useRef<HTMLDivElement>(null)

  const isWebResult = result && 'url' in result

  // Reset deploy state when result changes
  useEffect(() => {
    setDeployed(false)
    setSelectedDuration(null)
    setDeployedUrl(null)
  }, [result])

  // Close deploy dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (deployRef.current && !deployRef.current.contains(e.target as Node)) {
        setDeployOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  if (!stanseAgent) return null

  const handleCopy = () => {
    if (stanseAgent.code) {
      navigator.clipboard.writeText(stanseAgent.code)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  const handleDeploy = async () => {
    if (!selectedDuration || !result?.sbxId) return
    setIsDeploying(true)
    try {
      const { API_BASE, apiHeaders } = await import('../lib/api')
      await fetch(`${API_BASE}/api/publish`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...apiHeaders() },
        body: JSON.stringify({ sbxId: result.sbxId, duration: selectedDuration }),
      })
      setDeployed(true)
      setDeployOpen(false)
      if (isWebResult) {
        const u = (result as ExecutionResultWeb).url
        setDeployedUrl(u)
        // best-effort new tab; browsers often block window.open after an await (no direct
        // user gesture), so the visible link/banner below is the reliable way to open it.
        window.open(u, '_blank')
      }
    } catch (e) {
      console.error('Deploy failed:', e)
    } finally {
      setIsDeploying(false)
    }
  }

  // Stanse tab button style
  const tabStyle = (active: boolean): React.CSSProperties => ({
    padding: '4px 14px',
    fontSize: '10px',
    fontFamily: 'var(--font-mono)',
    fontWeight: 700,
    letterSpacing: '.06em',
    textTransform: 'uppercase',
    transition: 'all .1s',
    background: active ? '#111' : '#fff',
    color: active ? '#fff' : '#555',
    border: 'none',
    cursor: 'pointer',
  })

  return (
    <div style={{
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      background: '#fff',
      borderLeft: '3px solid #111',
    }}>
      {/* Header */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '8px 12px',
        borderBottom: '3px solid #111',
        background: '#f4f4f4',
      }}>
        {/* Close button */}
        <button
          onClick={onClose}
          style={{
            padding: '4px 8px',
            background: '#fff',
            border: '2px solid #111',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            boxShadow: '2px 2px 0 0 #111',
            transition: 'all .1s',
          }}
          title={t('common.close')}
        >
          <ChevronsRight style={{ width: 14, height: 14, color: '#111' }} />
        </button>

        {/* Code / Preview tabs */}
        <div style={{ display: 'flex', overflow: 'hidden', border: '2px solid #111' }}>
          <button
            onClick={() => setSelectedTab('code')}
            style={tabStyle(selectedTab === 'code')}
          >
            {isChatLoading && <LoaderCircle style={{ width: 10, height: 10, marginRight: 4, animation: 'spin 1s linear infinite', display: 'inline' }} />}
            {t('codePreview.code')}
          </button>
          <button
            onClick={() => result && setSelectedTab('preview')}
            disabled={!result}
            style={{
              ...tabStyle(selectedTab === 'preview'),
              borderLeft: '2px solid #111',
              opacity: result ? 1 : 0.3,
              cursor: result ? 'pointer' : 'not-allowed',
            }}
          >
            {t('codePreview.preview')}
            {isPreviewLoading && <LoaderCircle style={{ width: 10, height: 10, marginLeft: 4, animation: 'spin 1s linear infinite', display: 'inline' }} />}
          </button>
        </div>

        {/* Action buttons */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {/* Deploy to E2B */}
          {isWebResult && (
            <div style={{ position: 'relative' }} ref={deployRef}>
              <button
                onClick={() => !deployed && setDeployOpen(prev => !prev)}
                disabled={deployed}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                  padding: '4px 10px',
                  background: deployed ? '#f4f4f4' : '#111',
                  color: deployed ? '#22c55e' : '#fff',
                  border: '2px solid #111',
                  cursor: deployed ? 'default' : 'pointer',
                  fontFamily: 'var(--font-mono)',
                  fontSize: '10px',
                  fontWeight: 700,
                  letterSpacing: '.06em',
                  textTransform: 'uppercase',
                  boxShadow: deployed ? 'none' : '2px 2px 0 0 #555',
                  transition: 'all .1s',
                }}
              >
                <Rocket style={{ width: 12, height: 12 }} />
                {deployed ? t('codePreview.deployed') : t('codePreview.deploy')}
              </button>

              {/* Deploy dropdown — Stanse pixel-art style */}
              {deployOpen && (
                <div style={{
                  position: 'absolute',
                  right: 0,
                  top: '100%',
                  marginTop: 6,
                  zIndex: 50,
                  width: 240,
                  background: '#fff',
                  border: '2px solid #111',
                  boxShadow: 'var(--shadow-pixel)',
                }}>
                  {/* Title bar */}
                  <div style={{
                    padding: '8px 12px',
                    borderBottom: '2px solid #111',
                    background: '#111',
                    color: '#fff',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '10px',
                    fontWeight: 700,
                    letterSpacing: '.08em',
                    textTransform: 'uppercase',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                  }}>
                    <Rocket style={{ width: 11, height: 11 }} />
                    {t('codePreview.deployToE2B')}
                  </div>

                  {/* Description */}
                  <div style={{
                    padding: '8px 12px',
                    borderBottom: '1px solid #e5e5e5',
                    fontSize: '10px',
                    fontFamily: 'var(--font-mono)',
                    color: '#888',
                    lineHeight: 1.5,
                  }}>
                    {t('codePreview.deployDesc')}
                  </div>

                  {/* Duration options */}
                  <div style={{ padding: '6px 8px' }}>
                    <div style={{
                      fontSize: '9px',
                      fontFamily: 'var(--font-mono)',
                      fontWeight: 700,
                      color: '#999',
                      textTransform: 'uppercase',
                      letterSpacing: '.1em',
                      padding: '4px 4px 6px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 4,
                    }}>
                      <Clock style={{ width: 9, height: 9 }} />
                      {t('codePreview.selectDuration')}
                    </div>
                    {DURATION_OPTIONS.map(opt => (
                      <button
                        key={opt.value}
                        onClick={() => setSelectedDuration(opt.value)}
                        style={{
                          width: '100%',
                          textAlign: 'left',
                          padding: '6px 8px',
                          background: selectedDuration === opt.value ? '#111' : 'transparent',
                          color: selectedDuration === opt.value ? '#fff' : '#333',
                          border: selectedDuration === opt.value ? '2px solid #111' : '2px solid transparent',
                          cursor: 'pointer',
                          fontFamily: 'var(--font-mono)',
                          fontSize: '11px',
                          fontWeight: 600,
                          transition: 'all .1s',
                          marginBottom: 2,
                        }}
                        onMouseEnter={e => {
                          if (selectedDuration !== opt.value) {
                            (e.currentTarget as HTMLElement).style.background = '#f4f4f4'
                            ;(e.currentTarget as HTMLElement).style.border = '2px solid #ccc'
                          }
                        }}
                        onMouseLeave={e => {
                          if (selectedDuration !== opt.value) {
                            (e.currentTarget as HTMLElement).style.background = 'transparent'
                            ;(e.currentTarget as HTMLElement).style.border = '2px solid transparent'
                          }
                        }}
                      >
                        {t(opt.labelKey)}
                      </button>
                    ))}
                  </div>

                  {/* Deploy button */}
                  <div style={{ padding: '6px 8px 10px' }}>
                    <button
                      onClick={handleDeploy}
                      disabled={!selectedDuration || isDeploying}
                      style={{
                        width: '100%',
                        padding: '8px 0',
                        background: selectedDuration ? '#111' : '#ccc',
                        color: '#fff',
                        border: '2px solid #111',
                        cursor: selectedDuration && !isDeploying ? 'pointer' : 'not-allowed',
                        fontFamily: 'var(--font-mono)',
                        fontSize: '11px',
                        fontWeight: 700,
                        letterSpacing: '.06em',
                        textTransform: 'uppercase',
                        boxShadow: selectedDuration ? '2px 2px 0 0 #555' : 'none',
                        transition: 'all .1s',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        gap: 6,
                        opacity: selectedDuration ? 1 : 0.5,
                      }}
                    >
                      {isDeploying && <LoaderCircle style={{ width: 12, height: 12, animation: 'spin 1s linear infinite' }} />}
                      <Rocket style={{ width: 12, height: 12 }} />
                      {isDeploying ? t('codePreview.deploying') : t('codePreview.confirmDeploy')}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Copy button */}
          <button
            onClick={handleCopy}
            style={{
              padding: '4px 8px',
              background: '#fff',
              border: '2px solid #111',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              boxShadow: '2px 2px 0 0 #111',
              transition: 'all .1s',
            }}
          >
            {copied
              ? <Check style={{ width: 14, height: 14, color: '#22c55e' }} />
              : <Copy style={{ width: 14, height: 14, color: '#111' }} />
            }
          </button>
        </div>
      </div>

      {/* Deployed URL banner — reliable way to open the live app (window.open is often
          blocked by the popup blocker after the async deploy call). */}
      {deployedUrl && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px',
          background: '#f0fdf4', borderBottom: '2px solid #22c55e',
          fontFamily: 'var(--font-mono)', fontSize: '11px',
        }}>
          <Check style={{ width: 14, height: 14, color: '#22c55e', flexShrink: 0 }} />
          <span style={{ color: '#15803d', fontWeight: 700, flexShrink: 0 }}>{t('codePreview.deployed')}:</span>
          <a href={deployedUrl} target="_blank" rel="noopener noreferrer" style={{
            color: '#111', textDecoration: 'underline', overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1,
          }}>{deployedUrl}</a>
          <button
            onClick={() => { navigator.clipboard.writeText(deployedUrl); setUrlCopied(true); setTimeout(() => setUrlCopied(false), 2000) }}
            style={{ padding: '3px 6px', background: '#fff', border: '2px solid #111', cursor: 'pointer', flexShrink: 0, display: 'flex', alignItems: 'center' }}
            title="Copy URL"
          >{urlCopied ? <Check style={{ width: 12, height: 12, color: '#22c55e' }} /> : <Copy style={{ width: 12, height: 12, color: '#111' }} />}</button>
        </div>
      )}

      {/* Content */}
      <div style={{ flex: 1, overflowY: 'auto', background: '#fafafa' }}>
        {selectedTab === 'code' && stanseAgent.code && (
          <div style={{ padding: 16 }}>
            {stanseAgent.file_path && (
              <div style={{
                fontSize: '10px',
                color: '#999',
                marginBottom: 8,
                fontFamily: 'var(--font-mono)',
                textTransform: 'uppercase',
                letterSpacing: '.06em',
              }}>
                {stanseAgent.file_path}
              </div>
            )}
            <pre style={{
              fontSize: '12px',
              fontFamily: 'var(--font-mono)',
              color: '#111',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              lineHeight: 1.7,
              background: '#fff',
              border: '2px solid #e5e5e5',
              padding: 14,
            }}>
              {stanseAgent.code}
            </pre>
          </div>
        )}

        {selectedTab === 'preview' && result && (
          <div style={{ height: '100%' }}>
            {isWebResult ? (
              <iframe
                src={(result as ExecutionResultWeb).url}
                style={{ width: '100%', height: '100%', border: 'none' }}
                title="Preview"
                sandbox="allow-scripts allow-same-origin allow-forms"
              />
            ) : (
              <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
                {'stdout' in result && result.stdout && result.stdout.length > 0 && (
                  <div>
                    <div style={{
                      fontSize: '9px',
                      fontFamily: 'var(--font-mono)',
                      fontWeight: 700,
                      color: '#999',
                      textTransform: 'uppercase',
                      letterSpacing: '.1em',
                      marginBottom: 4,
                    }}>stdout</div>
                    <pre style={{
                      fontSize: '11px',
                      fontFamily: 'var(--font-mono)',
                      color: '#111',
                      background: '#fff',
                      border: '2px solid #e5e5e5',
                      padding: 12,
                      whiteSpace: 'pre-wrap',
                    }}>
                      {result.stdout.join('\n')}
                    </pre>
                  </div>
                )}
                {'stderr' in result && result.stderr && result.stderr.length > 0 && (
                  <div>
                    <div style={{
                      fontSize: '9px',
                      fontFamily: 'var(--font-mono)',
                      fontWeight: 700,
                      color: '#ef4444',
                      textTransform: 'uppercase',
                      letterSpacing: '.1em',
                      marginBottom: 4,
                    }}>stderr</div>
                    <pre style={{
                      fontSize: '11px',
                      fontFamily: 'var(--font-mono)',
                      color: '#dc2626',
                      background: '#fef2f2',
                      border: '2px solid #fca5a5',
                      padding: 12,
                      whiteSpace: 'pre-wrap',
                    }}>
                      {result.stderr.join('\n')}
                    </pre>
                  </div>
                )}
                {/* Runtime error / unsupported-environment guidance. Without this, a result
                    whose ONLY payload is a runtimeError (e.g. the tkinter/pygame GUI guard, or
                    any Python exception/traceback) renders a blank preview. */}
                {'runtimeError' in result && (result as any).runtimeError && (
                  <div>
                    <div style={{
                      fontSize: '9px', fontFamily: 'var(--font-mono)', fontWeight: 700,
                      color: '#ef4444', textTransform: 'uppercase', letterSpacing: '.1em', marginBottom: 4,
                    }}>{(result as any).runtimeError.name || 'error'}</div>
                    <pre style={{
                      fontSize: '12px', fontFamily: 'var(--font-mono)', color: '#dc2626',
                      background: '#fef2f2', border: '2px solid #fca5a5', padding: 12,
                      whiteSpace: 'pre-wrap', lineHeight: 1.6,
                    }}>
                      {[(result as any).runtimeError.value, (result as any).runtimeError.traceback]
                        .filter(Boolean).join('\n\n')}
                    </pre>
                  </div>
                )}
                {/* Jupyter cell outputs — matplotlib/plotly charts (png/jpeg/svg/html).
                    Without this, plot-only code (e.g. a matplotlib drawing with no stdout)
                    renders a blank preview. */}
                {'cellResults' in result && Array.isArray((result as any).cellResults) &&
                  (result as any).cellResults.map((cell: any, i: number) => {
                    if (cell?.png) return <img key={i} src={`data:image/png;base64,${cell.png}`} alt="output" style={{ maxWidth: '100%', border: '2px solid #e5e5e5' }} />
                    if (cell?.jpeg) return <img key={i} src={`data:image/jpeg;base64,${cell.jpeg}`} alt="output" style={{ maxWidth: '100%', border: '2px solid #e5e5e5' }} />
                    if (cell?.svg) return <div key={i} style={{ maxWidth: '100%' }} dangerouslySetInnerHTML={{ __html: cell.svg }} />
                    if (cell?.html) return <div key={i} style={{ maxWidth: '100%' }} dangerouslySetInnerHTML={{ __html: cell.html }} />
                    return null
                  })}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
