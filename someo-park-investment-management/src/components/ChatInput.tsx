import { ArrowUp, Paperclip, Square, X } from 'lucide-react'
import React, { SetStateAction, useEffect, useMemo, useState } from 'react'
import TextareaAutosize from 'react-textarea-autosize'
import { useTranslation } from 'react-i18next'

function isFileInArray(file: File, arr: File[]) {
  return arr.some(f => f.name === file.name && f.size === file.size && f.type === file.type)
}

export function ChatInput({
  retry,
  isErrored,
  errorMessage,
  isLoading,
  stop,
  input,
  handleInputChange,
  handleSubmit,
  isMultiModal,
  files,
  handleFileChange,
  children,
  placeholder,
}: {
  retry: () => void
  isErrored: boolean
  errorMessage: string
  isLoading: boolean
  stop: () => void
  input: string
  handleInputChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void
  handleSubmit: (e: React.FormEvent<HTMLFormElement>) => void
  isMultiModal: boolean
  files: File[]
  handleFileChange: (change: SetStateAction<File[]>) => void
  children: React.ReactNode
  placeholder?: string
}) {
  const { t } = useTranslation()

  function handleFileInput(e: React.ChangeEvent<HTMLInputElement>) {
    handleFileChange((prev) => {
      const newFiles = Array.from(e.target.files || []) as File[]
      return [...prev, ...newFiles.filter(f => !isFileInArray(f, prev))]
    })
  }

  function handleFileRemove(file: File) {
    handleFileChange((prev) => prev.filter(f => f !== file))
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    for (const item of Array.from(e.clipboardData.items) as DataTransferItem[]) {
      if (item.type.indexOf('image') !== -1) {
        e.preventDefault()
        const file = item.getAsFile()
        if (file) handleFileChange((prev) => isFileInArray(file, prev) ? prev : [...prev, file])
      }
    }
  }

  const [dragActive, setDragActive] = useState(false)
  const [toolbarVisible, setToolbarVisible] = useState(false)
  const hideTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null)

  function handleInputAreaEnter() {
    if (hideTimerRef.current) { clearTimeout(hideTimerRef.current); hideTimerRef.current = null; }
    setToolbarVisible(true)
  }

  function handleInputAreaLeave() {
    hideTimerRef.current = setTimeout(() => setToolbarVisible(false), 2000)
  }

  useEffect(() => {
    return () => { if (hideTimerRef.current) clearTimeout(hideTimerRef.current) }
  }, [])

  function handleDrag(e: React.DragEvent) {
    e.preventDefault(); e.stopPropagation()
    setDragActive(e.type === 'dragenter' || e.type === 'dragover')
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault(); e.stopPropagation()
    setDragActive(false)
    const dropped = Array.from(e.dataTransfer.files as FileList).filter(f => f.type.startsWith('image/'))
    if (dropped.length) handleFileChange(prev => [...prev, ...dropped.filter(f => !isFileInArray(f, prev))])
  }

  const filePreview = useMemo(() => {
    if (!files.length) return null
    return files.map(file => (
      <div key={file.name} style={{ position: 'relative', display: 'inline-block' }}>
        <button type="button" onClick={() => handleFileRemove(file)} style={{ position: 'absolute', top: -6, right: -6, width: 16, height: 16, background: '#111', border: '1px solid #111', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1 }}>
          <X style={{ width: 10, height: 10, color: '#fff' }} />
        </button>
        <img src={URL.createObjectURL(file)} alt={file.name} style={{ width: 40, height: 40, objectFit: 'cover', border: '2px solid #111', display: 'block' }} />
      </div>
    ))
  }, [files])

  function onEnter(e: React.KeyboardEvent<HTMLFormElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (e.currentTarget.checkValidity()) handleSubmit(e)
      else e.currentTarget.reportValidity()
    }
  }

  useEffect(() => {
    if (!isMultiModal) handleFileChange([])
  }, [isMultiModal])

  // Shared action button style (Stanse: p-3 border-2 border-black, flex-shrink-0)
  const actionBtnBase: React.CSSProperties = {
    flexShrink: 0,
    alignSelf: 'flex-end',
    padding: '12px 14px',
    border: '2px solid #111',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    transition: 'background .1s, transform .1s, box-shadow .1s',
    fontFamily: 'var(--font-mono)',
  }

  return (
    <form
      onSubmit={handleSubmit}
      onKeyDown={onEnter}
      style={{ marginBottom: 8, display: 'flex', flexDirection: 'column', position: 'relative' }}
      onDragEnter={isMultiModal ? handleDrag : undefined}
      onDragLeave={isMultiModal ? handleDrag : undefined}
      onDragOver={isMultiModal ? handleDrag : undefined}
      onDrop={isMultiModal ? handleDrop : undefined}
      onMouseEnter={handleInputAreaEnter}
      onMouseLeave={handleInputAreaLeave}
    >

      {/* Error bar */}
      {isErrored && (
        <div style={{ display: 'flex', alignItems: 'center', padding: '8px 12px', marginBottom: 10, background: '#fff0f0', border: '2px solid #ff3333', borderLeft: '4px solid #ff3333', fontFamily: 'var(--font-mono)', fontSize: '12px', color: '#ff3333', gap: 10 }}>
          <span style={{ flex: 1 }}>{errorMessage}</span>
          <button onClick={retry} type="button" style={{ padding: '3px 10px', fontFamily: 'var(--font-mono)', fontSize: '11px', fontWeight: 700, textTransform: 'uppercase', background: '#ff3333', color: '#fff', border: '2px solid #ff3333', cursor: 'pointer' }}>
            {t('chatInput.tryAgain')}
          </button>
        </div>
      )}

      {/* ── Toolbar row (model picker, settings) — floating above input ── */}
      <div
        onMouseEnter={handleInputAreaEnter}
        style={{
          position: 'absolute',
          bottom: '100%',
          left: 0,
          right: 0,
          zIndex: 20,
          display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
          background: '#f4f4f4',
          border: '2px solid #111',
          padding: '7px 10px',
          marginBottom: 4,
          boxShadow: '0 -2px 8px rgba(0,0,0,0.08)',
          opacity: toolbarVisible ? 1 : 0,
          pointerEvents: toolbarVisible ? 'auto' : 'none',
          transform: toolbarVisible ? 'translateY(0)' : 'translateY(6px)',
          transition: 'opacity .15s ease, transform .15s ease',
        }}
      >
        {children}
      </div>

      {/* ── File previews ── */}
      {files.length > 0 && (
        <div style={{ display: 'flex', gap: 8, padding: '8px 12px', flexWrap: 'wrap', background: '#fff', border: '2px solid #111', borderTop: 'none', borderBottom: '1px solid #e5e5e5' }}>
          {filePreview}
        </div>
      )}

      {/* ── Main input row — Stanse pattern: flex gap-2 items-end ── */}
      <div
        style={{
          display: 'flex',
          gap: 8,
          alignItems: 'flex-end',
          padding: '10px 12px',
          background: '#fff',
          border: '2px solid #111',
          borderTop: dragActive ? '2px dashed #111' : '2px solid #111',
          boxShadow: '4px 4px 0 0 #111',
        }}
      >
        {/* Textarea */}
        <TextareaAutosize
          autoFocus
          minRows={3}
          maxRows={8}
          style={{
            flex: 1,
            padding: '10px 12px',
            background: '#f9f9f9',
            border: '2px solid #ccc',
            outline: 'none',
            fontFamily: 'var(--font-mono)',
            fontSize: '13px',
            color: '#111',
            resize: 'none',
            lineHeight: '1.6',
            caretColor: '#111',
            transition: 'border-color .15s',
          }}
          required
          placeholder={placeholder || t('chatInput.placeholder')}
          disabled={isErrored}
          value={input}
          onChange={handleInputChange}
          onPaste={isMultiModal ? handlePaste : undefined}
          onFocus={e => { (e.currentTarget as HTMLElement).style.borderColor = '#111'; handleInputAreaEnter(); }}
          onBlur={e => {
            (e.currentTarget as HTMLElement).style.borderColor = '#ccc';
            // Don't hide toolbar if focus moved to another element within the form (e.g. toolbar buttons)
            const form = e.currentTarget.closest('form');
            if (form && e.relatedTarget && form.contains(e.relatedTarget as Node)) return;
            handleInputAreaLeave();
          }}
        />

        {/* Right-side action buttons — stacked vertically */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, flexShrink: 0 }}>

          {/* Attachment button */}
          <input type="file" id="multimodal" name="multimodal" accept="image/*" multiple style={{ display: 'none' }} onChange={handleFileInput} />
          <button
            disabled={!isMultiModal || isErrored}
            type="button"
            onClick={e => { e.preventDefault(); document.getElementById('multimodal')?.click() }}
            title="Attach image"
            style={{
              ...actionBtnBase,
              alignSelf: 'auto',
              background: (!isMultiModal || isErrored) ? '#f4f4f4' : '#fff',
              opacity: (!isMultiModal || isErrored) ? 0.35 : 1,
              cursor: (!isMultiModal || isErrored) ? 'not-allowed' : 'pointer',
              boxShadow: '2px 2px 0 0 #111',
            }}
            onMouseEnter={e => {
              if (isMultiModal && !isErrored) {
                (e.currentTarget as HTMLElement).style.background = '#111'
                ;(e.currentTarget as HTMLElement).style.transform = 'translate(-1px,-1px)'
                ;(e.currentTarget as HTMLElement).style.boxShadow = '3px 3px 0 0 #111'
              }
            }}
            onMouseLeave={e => {
              if (isMultiModal && !isErrored) {
                (e.currentTarget as HTMLElement).style.background = '#fff'
                ;(e.currentTarget as HTMLElement).style.transform = 'none'
                ;(e.currentTarget as HTMLElement).style.boxShadow = '2px 2px 0 0 #111'
              }
              // reset icon color
              const icon = (e.currentTarget as HTMLElement).querySelector('svg') as SVGElement
              if (icon) icon.style.color = '#555'
            }}
          >
            <Paperclip style={{ width: 16, height: 16, color: '#555', transition: 'color .1s' }} />
          </button>

          {/* Send / Stop button */}
          {!isLoading ? (
            <button
              disabled={isErrored}
              type="submit"
              title="Send"
              style={{
                ...actionBtnBase,
                alignSelf: 'auto',
                background: '#111',
                color: '#fff',
                boxShadow: '2px 2px 0 0 #555',
                opacity: isErrored ? 0.4 : 1,
                cursor: isErrored ? 'not-allowed' : 'pointer',
              }}
              onMouseEnter={e => {
                if (!isErrored) {
                  (e.currentTarget as HTMLElement).style.background = '#333'
                  ;(e.currentTarget as HTMLElement).style.transform = 'translate(-1px,-1px)'
                  ;(e.currentTarget as HTMLElement).style.boxShadow = '3px 3px 0 0 #555'
                }
              }}
              onMouseLeave={e => {
                ;(e.currentTarget as HTMLElement).style.background = '#111'
                ;(e.currentTarget as HTMLElement).style.transform = 'none'
                ;(e.currentTarget as HTMLElement).style.boxShadow = '2px 2px 0 0 #555'
              }}
            >
              <ArrowUp style={{ width: 18, height: 18, color: '#fff' }} />
            </button>
          ) : (
            <button
              type="button"
              onClick={e => { e.preventDefault(); stop() }}
              title="Stop"
              style={{
                ...actionBtnBase,
                alignSelf: 'auto',
                background: '#ff3333',
                color: '#fff',
                border: '2px solid #ff3333',
                boxShadow: '2px 2px 0 0 #cc0000',
                cursor: 'pointer',
              }}
              onMouseEnter={e => {
                ;(e.currentTarget as HTMLElement).style.background = '#cc0000'
                ;(e.currentTarget as HTMLElement).style.transform = 'translate(-1px,-1px)'
              }}
              onMouseLeave={e => {
                ;(e.currentTarget as HTMLElement).style.background = '#ff3333'
                ;(e.currentTarget as HTMLElement).style.transform = 'none'
              }}
            >
              <Square style={{ width: 18, height: 18, color: '#fff' }} />
            </button>
          )}
        </div>
      </div>

      {/* Hint */}
      <p style={{ fontSize: '9px', color: '#aaa', marginTop: 14, textAlign: 'center', fontFamily: 'var(--font-mono)', letterSpacing: '.08em', textTransform: 'uppercase' }}>
        SOMEOCLAW — SOMEO PARK AI AGENTS SYSTEM
      </p>
    </form>
  )
}
