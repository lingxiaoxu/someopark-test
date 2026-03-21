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
}) {
  const { t } = useTranslation()

  function handleFileInput(e: React.ChangeEvent<HTMLInputElement>) {
    handleFileChange((prev) => {
      const newFiles = Array.from(e.target.files || []) as File[]
      const uniqueFiles = newFiles.filter((file) => !isFileInArray(file, prev))
      return [...prev, ...uniqueFiles]
    })
  }

  function handleFileRemove(file: File) {
    handleFileChange((prev) => prev.filter((f) => f !== file))
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const items = Array.from(e.clipboardData.items) as DataTransferItem[]
    for (const item of items) {
      if (item.type.indexOf('image') !== -1) {
        e.preventDefault()
        const file = item.getAsFile()
        if (file) {
          handleFileChange((prev) => isFileInArray(file, prev) ? prev : [...prev, file])
        }
      }
    }
  }

  const [dragActive, setDragActive] = useState(false)

  function handleDrag(e: React.DragEvent) {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') setDragActive(true)
    else if (e.type === 'dragleave') setDragActive(false)
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    const droppedFiles = (Array.from(e.dataTransfer.files) as File[]).filter((f) => f.type.startsWith('image/'))
    if (droppedFiles.length > 0) {
      handleFileChange((prev) => [...prev, ...droppedFiles.filter((f) => !isFileInArray(f, prev))])
    }
  }

  const filePreview = useMemo(() => {
    if (files.length === 0) return null
    return files.map((file) => (
      <div className="relative" key={file.name}>
        <span onClick={() => handleFileRemove(file)} className="absolute -top-2 -right-2 bg-[var(--bg-tertiary)] rounded-full p-0.5 cursor-pointer hover:bg-[var(--bg-secondary)]">
          <X className="h-3 w-3 text-[var(--text-muted)]" />
        </span>
        <img src={URL.createObjectURL(file)} alt={file.name} className="rounded-lg w-10 h-10 object-cover" />
      </div>
    ))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files])

  function onEnter(e: React.KeyboardEvent<HTMLFormElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (e.currentTarget.checkValidity()) {
        handleSubmit(e)
      } else {
        e.currentTarget.reportValidity()
      }
    }
  }

  useEffect(() => {
    if (!isMultiModal) handleFileChange([])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMultiModal])

  return (
    <form
      onSubmit={handleSubmit}
      onKeyDown={onEnter}
      className="mb-2 mt-auto flex flex-col"
      onDragEnter={isMultiModal ? handleDrag : undefined}
      onDragLeave={isMultiModal ? handleDrag : undefined}
      onDragOver={isMultiModal ? handleDrag : undefined}
      onDrop={isMultiModal ? handleDrop : undefined}
    >
      {isErrored && (
        <div className="flex items-center p-2 text-sm font-medium mx-4 mb-3 rounded-xl bg-red-400/10 text-red-400">
          <span className="flex-1 px-1.5">{errorMessage}</span>
          <button className="px-2 py-1 rounded-md bg-red-400/20 hover:bg-red-400/30" onClick={retry}>
            {t('chatInput.tryAgain')}
          </button>
        </div>
      )}
      <div className="relative">
        <div className={`rounded-2xl relative z-10 bg-[var(--bg-primary)] border border-[var(--border-subtle)] shadow-sm ${dragActive ? 'border-[var(--accent-primary)] border-dashed' : ''}`}>
          <div className="flex items-center px-3 py-2 gap-1">{children}</div>
          <TextareaAutosize
            autoFocus={true}
            minRows={1}
            maxRows={5}
            className="text-sm px-3 resize-none ring-0 bg-inherit w-full m-0 outline-none text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
            required={true}
            placeholder={t('chatInput.placeholder')}
            disabled={isErrored}
            value={input}
            onChange={handleInputChange}
            onPaste={isMultiModal ? handlePaste : undefined}
          />
          <div className="flex p-3 gap-2 items-center">
            <input type="file" id="multimodal" name="multimodal" accept="image/*" multiple className="hidden" onChange={handleFileInput} />
            <div className="flex items-center flex-1 gap-2">
              <button
                disabled={!isMultiModal || isErrored}
                type="button"
                className="p-2 rounded-lg border border-[var(--border-subtle)] text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-secondary)] transition-colors disabled:opacity-30"
                onClick={(e) => { e.preventDefault(); document.getElementById('multimodal')?.click() }}
              >
                <Paperclip className="h-4 w-4" />
              </button>
              {files.length > 0 && filePreview}
            </div>
            <div>
              {!isLoading ? (
                <button
                  disabled={isErrored}
                  type="submit"
                  className="p-2 rounded-lg bg-[var(--accent-primary)] text-white hover:opacity-90 transition-opacity disabled:opacity-30"
                >
                  <ArrowUp className="h-4 w-4" />
                </button>
              ) : (
                <button
                  type="button"
                  className="p-2 rounded-lg bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)]"
                  onClick={(e) => { e.preventDefault(); stop() }}
                >
                  <Square className="h-4 w-4" />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
      <p className="text-[10px] text-[var(--text-muted)] mt-2 text-center">
        SomeoClaw - Someo Park AI Assistant
      </p>
    </form>
  )
}
