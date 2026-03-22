const { onDocumentCreated } = require('firebase-functions/v2/firestore');
const { defineSecret } = require('firebase-functions/params');
const { initializeApp } = require('firebase-admin/app');
const { getFirestore, FieldValue } = require('firebase-admin/firestore');

initializeApp();

const BOT_TOKEN = defineSecret('BOT_TOKEN');
const BOT_CHAT_ID = defineSecret('BOT_CHAT_ID');

/**
 * Triggered when a new document is created in bot_commands/{docId}.
 * Sends a Telegram message to the VPS bot with the command text and [CMD:docId] tag.
 */
exports.onBotCommand = onDocumentCreated(
  {
    document: 'bot_commands/{docId}',
    secrets: [BOT_TOKEN, BOT_CHAT_ID],
  },
  async (event) => {
    const docId = event.params.docId;
    const data = event.data?.data();
    if (!data) return;

    const command = data.command || '';
    const db = getFirestore();
    const docRef = db.collection('bot_commands').doc(docId);

    const botToken = BOT_TOKEN.value();
    const chatId = BOT_CHAT_ID.value();

    if (!botToken || !chatId) {
      console.error('Missing BOT_TOKEN or BOT_CHAT_ID secrets');
      await docRef.update({ status: 'error', error: 'Missing bot secrets' });
      return;
    }

    // Message format: command text + [CMD:docId] so VPS knows which doc to reply to
    const text = `${command}\n[CMD:${docId}]`;

    try {
      await sendTelegramMessage(botToken, chatId, text);
      await docRef.update({ status: 'sent', sentAt: FieldValue.serverTimestamp() });
      console.log(`Sent command ${docId} to Telegram`);
    } catch (err) {
      console.error('Failed to send Telegram message:', err);
      await docRef.update({ status: 'error', error: String(err) });
    }
  }
);

async function sendTelegramMessage(botToken, chatId, text) {
  const url = `https://api.telegram.org/bot${botToken}/sendMessage`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.description || 'Telegram API error');
  return data;
}
