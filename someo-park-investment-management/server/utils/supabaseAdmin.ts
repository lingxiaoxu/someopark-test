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
