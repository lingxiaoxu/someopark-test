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
      <div className="absolute inset-0 bg-black/60 " onClick={() => setOpen(false)} />
      <div className="relative w-full max-w-md mx-4 p-6 rounded-none bg-white border-2 border-black shadow-none">
        <button
          onClick={() => setOpen(false)}
          className="absolute top-4 right-4 p-1 rounded-none text-gray-500 hover:text-black hover:bg-[#e5e5e5] transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
        <div className="flex items-center gap-3 mb-6">
          <div className="flex items-center justify-center rounded-none bg-[#e5e5e5] p-2 border-2 border-black">
            <Terminal className="w-5 h-5 text-black" />
          </div>
          <h2 className="text-lg font-semibold text-black">
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
