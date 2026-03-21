import { supabase } from './supabase'
import { Conversation } from './types'

export async function listConversations(userId: string): Promise<Conversation[]> {
  if (!supabase) return []
  const { data, error } = await supabase
    .from('conversations')
    .select('*')
    .eq('user_id', userId)
    .order('updated_at', { ascending: false })
  if (error) { console.error('listConversations error:', error); return [] }
  return data || []
}

export async function getConversation(id: string) {
  if (!supabase) return null
  const { data, error } = await supabase
    .from('conversations')
    .select('*')
    .eq('id', id)
    .single()
  if (error) { console.error('getConversation error:', error); return null }
  return data
}

export async function createConversation(userId: string, title = 'New Chat'): Promise<Conversation | null> {
  if (!supabase) return null
  const { data, error } = await supabase
    .from('conversations')
    .insert({ user_id: userId, title })
    .select()
    .single()
  if (error) { console.error('createConversation error:', error); return null }
  return data
}

export async function deleteConversation(id: string) {
  if (!supabase) return
  const { error } = await supabase
    .from('conversations')
    .delete()
    .eq('id', id)
  if (error) console.error('deleteConversation error:', error)
}

export async function updateConversationTitle(id: string, title: string) {
  if (!supabase) return
  const { error } = await supabase
    .from('conversations')
    .update({ title, updated_at: new Date().toISOString() })
    .eq('id', id)
  if (error) console.error('updateTitle error:', error)
}

export async function saveMessage(conversationId: string, role: string, content: string, artifacts?: any[], object?: any, result?: any) {
  if (!supabase) return
  const { error } = await supabase
    .from('messages')
    .insert({
      conversation_id: conversationId,
      role,
      content,
      artifacts: artifacts || [],
      object: object || null,
      result: result || null,
    })
  if (error) console.error('saveMessage error:', error)

  // Update conversation timestamp
  await supabase
    .from('conversations')
    .update({ updated_at: new Date().toISOString() })
    .eq('id', conversationId)
}

export async function loadMessages(conversationId: string) {
  if (!supabase) return []
  const { data, error } = await supabase
    .from('messages')
    .select('*')
    .eq('conversation_id', conversationId)
    .order('created_at', { ascending: true })
  if (error) { console.error('loadMessages error:', error); return [] }
  return data || []
}
