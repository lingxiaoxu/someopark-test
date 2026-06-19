// src/lib/chatStore.ts
// Per-user chat history synced to Supabase so it follows the user across devices /
// re-logins. One row per chat in `user_chats`, isolated by Row Level Security
// (auth.uid() = user_id). Uses the browser supabase client + the user's session;
// the anon key is safe here because RLS restricts every row to its owner.
// All calls fail soft (log + continue) so the app still works on localStorage alone
// if the table/policies aren't set up or the network is down.

import type { SupabaseClient } from '@supabase/supabase-js'

export type StoredChat = { id: number; title: string; messages: any[] }

export async function loadUserChats(
  supabase: SupabaseClient,
  userId: string,
): Promise<StoredChat[]> {
  const { data, error } = await supabase
    .from('user_chats')
    .select('id, title, messages')
    .eq('user_id', userId)
    .order('updated_at', { ascending: false })
  if (error) {
    console.warn('[chatStore] load failed:', error.message)
    return []
  }
  return (data || []).map(r => ({
    id: Number(r.id),
    title: r.title || '',
    messages: Array.isArray(r.messages) ? r.messages : [],
  }))
}

export async function upsertUserChat(
  supabase: SupabaseClient,
  userId: string,
  chat: StoredChat,
): Promise<void> {
  const { error } = await supabase.from('user_chats').upsert(
    {
      user_id: userId,
      id: chat.id,
      title: chat.title,
      messages: chat.messages,
      updated_at: new Date().toISOString(),
    },
    { onConflict: 'user_id,id' },
  )
  if (error) console.warn('[chatStore] upsert failed:', error.message)
}

export async function deleteUserChat(
  supabase: SupabaseClient,
  userId: string,
  id: number,
): Promise<void> {
  const { error } = await supabase
    .from('user_chats')
    .delete()
    .eq('user_id', userId)
    .eq('id', id)
  if (error) console.warn('[chatStore] delete failed:', error.message)
}
