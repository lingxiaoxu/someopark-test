import Auth, { ViewType } from './Auth'
import { SupabaseClient } from '@supabase/supabase-js'
import { X, Terminal } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export function AuthDialog({
  open,
  setOpen,
  supabase,
  view,
}: {
  open: boolean
  setOpen: (open: boolean) => void
  supabase: SupabaseClient
  view: ViewType
}) {
  const { t } = useTranslation()

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setOpen(false)} />
      <div className="relative w-full max-w-md mx-4 p-6 rounded-2xl bg-[var(--bg-primary)] border border-[var(--border-subtle)] shadow-2xl">
        <button
          onClick={() => setOpen(false)}
          className="absolute top-4 right-4 p-1 rounded-md text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
        <div className="flex items-center gap-3 mb-6">
          <div className="flex items-center justify-center rounded-lg bg-[var(--bg-tertiary)] p-2 border border-[var(--border-subtle)]">
            <Terminal className="w-5 h-5 text-[var(--accent-primary)]" />
          </div>
          <h2 className="text-lg font-semibold text-[var(--text-primary)]">
            {t('auth.signInToSomeoPark')}
          </h2>
        </div>
        <Auth
          supabaseClient={supabase}
          view={view}
          providers={['github', 'google']}
        />
      </div>
    </div>
  )
}
