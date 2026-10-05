// server/utils/supabaseAdmin.ts
// Server-side Supabase client using the secret (service-role) key. Bypasses RLS.
// Used by the Someo Agent usage gate: verify a user's JWT -> real email, and
// read/increment per-user question counts in the `agent_usage` table.
// Key comes from env only (.env, gitignored) — never hardcode / commit.

import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const url = process.env.SUPABASE_URL || process.env.VITE_SUPABASE_URL
const secret = process.env.SUPABASE_SECRET_KEY

export const supabaseAdmin: SupabaseClient | null =
  url && secret
    ? createClient(url, secret, { auth: { persistSession: false, autoRefreshToken: false } })
    : null

if (!supabaseAdmin) {
  console.warn('[supabaseAdmin] SUPABASE_URL / SUPABASE_SECRET_KEY not set — agent usage gate disabled (all users unlimited).')
}

// Verify a Supabase access token (JWT) and return the user's email, or null.
export async function emailFromToken(accessToken?: string): Promise<string | null> {
  if (!supabaseAdmin || !accessToken) return null
  try {
    const { data, error } = await supabaseAdmin.auth.getUser(accessToken)
    if (error) return null
    return data.user?.email ?? null
  } catch {
    return null
  }
}

// Verify a Supabase access token and return { id, email }, or null.
// The id (auth.users.id) is the ONLY identity the Kalshi-key routes trust —
// never a client-supplied user id.
export type SupabaseIdentity = {
  id: string
  email: string | null
  email_confirmed?: boolean
  provider?: string | null
}

function toIdentity(u: any): SupabaseIdentity | null {
  if (!u?.id) return null
  return {
    id: u.id,
    email: u.email ?? null,
    email_confirmed: !!u.email_confirmed_at,
    provider: u.app_metadata?.provider ?? null,
  }
}

export async function userFromToken(accessToken?: string): Promise<SupabaseIdentity | null> {
  if (!supabaseAdmin || !accessToken) return null
  try {
    const { data, error } = await supabaseAdmin.auth.getUser(accessToken)
    if (error) return null
    return toIdentity(data.user)
  } catch {
    return null
  }
}

// Re-read one user straight from auth.users by id (service key) - the
// independent cross-check of id + email before a Kalshi key is activated.
export async function supabaseUserById(id: string): Promise<SupabaseIdentity | null> {
  if (!supabaseAdmin) return null
  try {
    const { data, error } = await supabaseAdmin.auth.admin.getUserById(id)
    if (error) return null
    return toIdentity(data.user)
  } catch {
    return null
  }
}
