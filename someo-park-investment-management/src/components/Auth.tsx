import { SupabaseClient, Provider } from '@supabase/supabase-js'
import { AlertCircle, CheckCircle2, KeyRound, Loader2, Mail } from 'lucide-react'
import React, { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

const VIEWS = {
  SIGN_IN: 'sign_in',
  SIGN_UP: 'sign_up',
  FORGOTTEN_PASSWORD: 'forgotten_password',
  MAGIC_LINK: 'magic_link',
  UPDATE_PASSWORD: 'update_password',
} as const

export type ViewType = (typeof VIEWS)[keyof typeof VIEWS]

export interface AuthProps {
  supabaseClient: SupabaseClient
  providers?: Provider[]
  view?: ViewType
  redirectTo?: string
}

function useAuthForm() {
  const [loading, setLoading] = useState(false)
  const [error, setErrorState] = useState<string | null>(null)
  const [message, setMessageState] = useState<string | null>(null)

  const setError = useCallback((errorMsg: string | null) => {
    setErrorState(errorMsg)
    if (errorMsg) setMessageState(null)
  }, [])

  const setMessage = useCallback((msg: string | null) => {
    setMessageState(msg)
    if (msg) setErrorState(null)
  }, [])

  const clearMessages = useCallback(() => {
    setErrorState(null)
    setMessageState(null)
  }, [])

  return { loading, error, message, setLoading, setError, setMessage, clearMessages }
}

function SocialAuth({
  supabaseClient,
  providers,
  redirectTo,
  setLoading,
  setError,
  clearMessages,
  loading,
}: {
  supabaseClient: SupabaseClient
  providers: Provider[]
  redirectTo?: string
  setLoading: (l: boolean) => void
  setError: (e: string) => void
  clearMessages: () => void
  loading: boolean
}) {
  const handleProviderSignIn = async (provider: Provider) => {
    clearMessages()
    setLoading(true)
    const { error } = await supabaseClient.auth.signInWithOAuth({
      provider,
      options: { redirectTo },
    })
    if (error) setError(error.message)
  }

  return (
    <div className="flex gap-3">
      {providers.map((provider) => (
        <button
          key={provider}
          onClick={() => handleProviderSignIn(provider)}
          disabled={loading}
          className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-secondary)] text-sm text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors disabled:opacity-50"
        >
          {provider.charAt(0).toUpperCase() + provider.slice(1)}
        </button>
      ))}
    </div>
  )
}

function SignInForm({
  supabaseClient,
  setAuthView,
  setLoading,
  setError,
  clearMessages,
  loading,
}: {
  supabaseClient: SupabaseClient
  setAuthView: (v: ViewType) => void
  setLoading: (l: boolean) => void
  setError: (e: string | null) => void
  clearMessages: () => void
  loading: boolean
}) {
  const { t } = useTranslation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault()
    clearMessages()
    setLoading(true)
    try {
      const { error } = await supabaseClient.auth.signInWithPassword({ email, password })
      if (error) throw error
    } catch (error: any) {
      setError(error.message || 'An unexpected error occurred.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSignIn} className="space-y-4">
      <div className="space-y-1.5">
        <label className="text-sm text-[var(--text-secondary)]">{t('auth.email')}</label>
        <div className="relative">
          <Mail className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input type="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email"
            className="w-full pl-10 pr-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
        </div>
      </div>
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <label className="text-sm text-[var(--text-secondary)]">{t('auth.password')}</label>
          <button type="button" onClick={() => setAuthView(VIEWS.FORGOTTEN_PASSWORD)} className="text-xs text-[var(--text-muted)] hover:text-[var(--accent-primary)]">
            {t('auth.forgotPassword')}
          </button>
        </div>
        <div className="relative">
          <KeyRound className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input type="password" placeholder="********" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password"
            className="w-full pl-10 pr-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
        </div>
      </div>
      <button type="submit" disabled={loading} className="w-full py-2.5 rounded-lg bg-[var(--accent-primary)] text-white text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-50 flex items-center justify-center gap-2">
        {loading && <Loader2 className="w-4 h-4 animate-spin" />}
        {t('auth.signIn')}
      </button>
    </form>
  )
}

function SignUpForm({
  supabaseClient,
  setLoading,
  setError,
  setMessage,
  clearMessages,
  loading,
  redirectTo,
}: {
  supabaseClient: SupabaseClient
  setLoading: (l: boolean) => void
  setError: (e: string | null) => void
  setMessage: (m: string | null) => void
  clearMessages: () => void
  loading: boolean
  redirectTo?: string
}) {
  const { t } = useTranslation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')

  const handleSignUp = async (e: React.FormEvent) => {
    e.preventDefault()
    clearMessages()
    setLoading(true)
    try {
      if (password !== confirmPassword) throw new Error('Passwords do not match')
      const { data, error } = await supabaseClient.auth.signUp({
        email, password,
        options: { emailRedirectTo: redirectTo },
      })
      if (error) throw error
      if (data.user && !data.session) {
        setMessage(t('auth.checkEmail'))
      }
    } catch (error: any) {
      setError(error.message || 'An unexpected error occurred.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSignUp} className="space-y-4">
      <div className="space-y-1.5">
        <label className="text-sm text-[var(--text-secondary)]">{t('auth.email')}</label>
        <div className="relative">
          <Mail className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input type="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email"
            className="w-full pl-10 pr-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
        </div>
      </div>
      <div className="space-y-1.5">
        <label className="text-sm text-[var(--text-secondary)]">{t('auth.password')}</label>
        <div className="relative">
          <KeyRound className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input type="password" placeholder="********" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="new-password"
            className="w-full pl-10 pr-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
        </div>
      </div>
      <div className="space-y-1.5">
        <label className="text-sm text-[var(--text-secondary)]">{t('auth.confirmPassword')}</label>
        <div className="relative">
          <KeyRound className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input type="password" placeholder="********" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} required autoComplete="new-password"
            className="w-full pl-10 pr-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
        </div>
      </div>
      <button type="submit" disabled={loading} className="w-full py-2.5 rounded-lg bg-[var(--accent-primary)] text-white text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-50 flex items-center justify-center gap-2">
        {loading && <Loader2 className="w-4 h-4 animate-spin" />}
        {t('auth.signUp')}
      </button>
    </form>
  )
}

function ForgottenPassword({
  supabaseClient,
  setLoading,
  setError,
  setMessage,
  clearMessages,
  loading,
  redirectTo,
}: {
  supabaseClient: SupabaseClient
  setLoading: (l: boolean) => void
  setError: (e: string | null) => void
  setMessage: (m: string | null) => void
  clearMessages: () => void
  loading: boolean
  redirectTo?: string
}) {
  const { t } = useTranslation()
  const [email, setEmail] = useState('')

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault()
    clearMessages()
    setLoading(true)
    const { error } = await supabaseClient.auth.resetPasswordForEmail(email, { redirectTo })
    if (error) setError(error.message)
    else setMessage(t('auth.checkEmailReset'))
    setLoading(false)
  }

  return (
    <form onSubmit={handleReset} className="space-y-4">
      <div className="space-y-1.5">
        <label className="text-sm text-[var(--text-secondary)]">{t('auth.email')}</label>
        <div className="relative">
          <Mail className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input type="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email"
            className="w-full pl-10 pr-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
        </div>
      </div>
      <button type="submit" disabled={loading} className="w-full py-2.5 rounded-lg bg-[var(--accent-primary)] text-white text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-50 flex items-center justify-center gap-2">
        {loading && <Loader2 className="w-4 h-4 animate-spin" />}
        {t('auth.sendResetInstructions')}
      </button>
    </form>
  )
}

function UpdatePassword({
  supabaseClient,
  setLoading,
  setError,
  setMessage,
  clearMessages,
  loading,
}: {
  supabaseClient: SupabaseClient
  setLoading: (l: boolean) => void
  setError: (e: string | null) => void
  setMessage: (m: string | null) => void
  clearMessages: () => void
  loading: boolean
}) {
  const { t } = useTranslation()
  const [password, setPassword] = useState('')

  const handleUpdate = async (e: React.FormEvent) => {
    e.preventDefault()
    clearMessages()
    setLoading(true)
    const { error } = await supabaseClient.auth.updateUser({ password })
    if (error) setError(error.message)
    else setMessage(t('auth.passwordUpdated'))
    setLoading(false)
    if (!error) setPassword('')
  }

  return (
    <form onSubmit={handleUpdate} className="space-y-4">
      <h3 className="text-lg font-semibold text-[var(--text-primary)]">{t('auth.updatePassword')}</h3>
      <div className="space-y-1.5">
        <label className="text-sm text-[var(--text-secondary)]">{t('auth.newPassword')}</label>
        <div className="relative">
          <KeyRound className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input type="password" placeholder="Enter new password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="new-password"
            className="w-full pl-10 pr-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
        </div>
      </div>
      <button type="submit" disabled={loading} className="w-full py-2.5 rounded-lg bg-[var(--accent-primary)] text-white text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-50 flex items-center justify-center gap-2">
        {loading && <Loader2 className="w-4 h-4 animate-spin" />}
        {t('auth.updatePassword')}
      </button>
    </form>
  )
}

export default function Auth({ supabaseClient, providers, view = VIEWS.SIGN_IN, redirectTo }: AuthProps) {
  const { t } = useTranslation()
  const [authView, setAuthView] = useState<ViewType>(view)
  const { loading, error, message, setLoading, setError, setMessage, clearMessages } = useAuthForm()

  useEffect(() => {
    setAuthView(view)
    setError(null)
    setMessage(null)
  }, [view, setError, setMessage])

  const setAuthViewClear = useCallback((v: ViewType) => {
    setAuthView(v)
    setError(null)
    setMessage(null)
  }, [setError, setMessage])

  const commonProps = { supabaseClient, setLoading, setError, setMessage, clearMessages, loading, redirectTo }

  let viewComponent: React.ReactNode = null

  switch (authView) {
    case VIEWS.SIGN_IN:
      viewComponent = <SignInForm {...commonProps} setAuthView={setAuthViewClear} />
      break
    case VIEWS.SIGN_UP:
      viewComponent = <SignUpForm {...commonProps} />
      break
    case VIEWS.FORGOTTEN_PASSWORD:
      viewComponent = <ForgottenPassword {...commonProps} />
      break
    case VIEWS.UPDATE_PASSWORD:
      viewComponent = <UpdatePassword {...commonProps} />
      break
  }

  const showSocialAuth = providers && providers.length > 0

  return (
    <div className="w-full space-y-4">
      {authView === VIEWS.UPDATE_PASSWORD ? viewComponent : (
        <>
          {showSocialAuth && (
            <SocialAuth supabaseClient={supabaseClient} providers={providers || []} redirectTo={redirectTo}
              setLoading={setLoading} setError={setError} clearMessages={clearMessages} loading={loading} />
          )}
          {showSocialAuth && (
            <div className="relative my-4">
              <div className="absolute inset-0 flex items-center"><div className="w-full border-t border-[var(--border-subtle)]" /></div>
              <div className="relative flex justify-center text-xs"><span className="bg-[var(--bg-primary)] px-2 text-[var(--text-muted)]">{t('auth.orContinueWith')}</span></div>
            </div>
          )}
          {viewComponent}
        </>
      )}

      {authView !== VIEWS.UPDATE_PASSWORD && (
        <div className="text-center text-sm space-y-1 mt-4">
          {authView === VIEWS.SIGN_IN && (
            <p className="text-[var(--text-muted)]">
              {t('auth.noAccount')}{' '}
              <button type="button" onClick={() => setAuthViewClear(VIEWS.SIGN_UP)} className="text-[var(--accent-primary)] hover:underline">{t('auth.signUp')}</button>
            </p>
          )}
          {authView === VIEWS.SIGN_UP && (
            <p className="text-[var(--text-muted)]">
              {t('auth.hasAccount')}{' '}
              <button type="button" onClick={() => setAuthViewClear(VIEWS.SIGN_IN)} className="text-[var(--accent-primary)] hover:underline">{t('auth.signIn')}</button>
            </p>
          )}
          {authView === VIEWS.FORGOTTEN_PASSWORD && (
            <button type="button" onClick={() => setAuthViewClear(VIEWS.SIGN_IN)} className="text-[var(--accent-primary)] hover:underline">{t('auth.backToSignIn')}</button>
          )}
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-red-500/10 text-red-400 text-sm">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {message && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-green-500/10 text-green-400 text-sm">
          <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{message}</span>
        </div>
      )}
    </div>
  )
}
