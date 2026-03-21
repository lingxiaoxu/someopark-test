import { supabase } from '../lib/supabase'
import { Session } from '@supabase/supabase-js'
import { useState, useEffect } from 'react'

export type ViewType = 'sign_in' | 'sign_up' | 'forgotten_password' | 'magic_link' | 'update_password'

export function useAuth(
  setAuthDialog: (value: boolean) => void,
  setAuthView: (value: ViewType) => void,
) {
  const [session, setSession] = useState<Session | null>(null)
  const [recovery, setRecovery] = useState(false)

  useEffect(() => {
    if (!supabase) {
      // No Supabase configured — use demo session
      setSession({ user: { email: 'demo@someopark.com' } } as Session)
      return
    }

    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session)
    })

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      setSession(session)

      if (_event === 'PASSWORD_RECOVERY') {
        setRecovery(true)
        setAuthView('update_password')
        setAuthDialog(true)
      }

      if (_event === 'USER_UPDATED' && recovery) {
        setRecovery(false)
      }

      if (_event === 'SIGNED_IN' && !recovery) {
        setAuthDialog(false)
      }

      if (_event === 'SIGNED_OUT') {
        setAuthView('sign_in')
        setRecovery(false)
      }
    })

    return () => subscription.unsubscribe()
  }, [recovery, setAuthDialog, setAuthView])

  return { session }
}
