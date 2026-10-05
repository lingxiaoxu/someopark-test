// Per-user Kalshi PROD credentials — client side (2026-10-01).
// Identity is the Supabase session; the server re-verifies the JWT and never
// trusts a client-supplied user id. Client checks below only give fast
// feedback — the server repeats every one of them.
import { useEffect, useState } from 'react';
import { API_BASE, apiHeaders } from './api';
import { supabase } from './supabase';
import { usesOwnKalshi, withUserKalshiProd, type KalshiStatus, type KalshiUserStore, type TradingState } from './kalshiUserPolicy';

export { usesOwnKalshi, withUserKalshiProd };
export type { KalshiStatus, KalshiUserStore };
export type { TradingState };

export const OWNER_EMAIL = 'lxu912@gmail.com';
export const KEY_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const PEM_MIN_BYTES = 80;    // must match server/utils/kalshiUserKeys.ts: an Ed25519 PKCS#8 PEM (Kalshi's default since 2026-10-01) is ~119 bytes
export const PEM_MAX_BYTES = 4096;
// Live-trading application (2026-10-04). Must match server/utils/kalshiUserKeys.ts
// (MIRROR_RATIOS, TRADING_ACK_VERSION) and crypto_common/config.py MIRROR_RATIOS.
export const MIRROR_RATIOS = [0.1, 0.25, 0.5, 1] as const;
export const TRADING_ACK_VERSION = '2026-10-04';


async function session() {
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session ?? null;
}

const isOwnerEmail = (email?: string | null) => (email ?? '').toLowerCase() === OWNER_EMAIL;

async function call<T>(method: string, path: string, body?: unknown, timeoutMs = 15_000): Promise<T> {
  const t = (await session())?.access_token;
  if (!t) throw new Error('请先登录');
  const res = await fetch(`${API_BASE}/api/kalshi-keys${path}`, {
    method,
    cache: 'no-store',
    signal: AbortSignal.timeout(timeoutMs),
    headers: { ...apiHeaders(), Authorization: `Bearer ${t}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((json as any)?.error || `请求失败（HTTP ${res.status}）`);
  return json as T;
}

export function checkKeyIdLocal(raw: string): string | null {
  if (!raw) return 'API Key 不能为空';
  if (raw !== raw.trim()) return 'API Key 前后不能有空格或换行';
  if (!KEY_ID_RE.test(raw)) return 'API Key 格式不正确：应为 36 位小写 UUID';
  return null;
}

export function checkPemFileLocal(files: FileList | null): string | null {
  if (!files || files.length === 0) return '请选择文件';
  if (files.length !== 1) return '只能上传一个文件';
  const f = files[0];
  if (!/\.(pem|txt|key)$/i.test(f.name)) return '文件类型错误：只接受 Kalshi 下载的私钥文件（.txt 或 .pem）';
  if (f.size < PEM_MIN_BYTES || f.size > PEM_MAX_BYTES) return `文件大小异常（${f.size} 字节）：私钥文件应在 ${PEM_MIN_BYTES}–${PEM_MAX_BYTES} 字节之间`;
  return null;
}

export const kalshiUser = {
  status: () => call<KalshiStatus>('GET', '/status'),
  saveKeyId: (key_id: string) => call<{ ok: true; key_id_masked: string }>('POST', '/key-id', { key_id }),
  async uploadPem(file: File) {
    const content = await file.text();
    return call<{ ok: true }>('POST', '/pem', { filename: file.name, content });
  },
  verify: () => call<{ ok: true; balance_usd: number; instance2_usd: number | null }>('POST', '/verify', undefined, 30_000),
  disconnect: () => call<{ ok: true }>('DELETE', '/'),
  balance: () => call<{ source: 'owner' | 'user'; balance_usd?: number; instance2_usd?: number | null; error?: string }>('GET', '/balance'),
  ledger: () => call<any>('GET', '/ledger'),
  requestTrading: (ratio: number) => call<{ ok: true; trading: TradingState }>('POST', '/trading/request',
    { ratio, ack: true, ack_version: TRADING_ACK_VERSION }),
  cancelTradingRequest: () => call<{ ok: true; trading: TradingState }>('DELETE', '/trading/request'),
  stopTrading: () => call<{ ok: true; trading: TradingState }>('POST', '/trading/stop'),
};

// ── shared store: status + balance, refreshed on login change and after edits
// `ready` stays false until the status for the CURRENT session is known, so a
// connected user is never shown the owner's figures while it loads. Every
// refresh carries a generation; an answer started under an older session
// (e.g. before sign-out) is discarded instead of overwriting the store.
type Store = KalshiUserStore & { ready: boolean };
let store: Store = { status: null, balanceUsd: null, ready: false };
const listeners = new Set<(s: Store) => void>();
let generation = 0;
let inFlight = false;

function publish(next: Store) {
  store = next;
  listeners.forEach(l => l(store));
}

export async function refreshKalshiUser(reset = false) {
  if (inFlight && !reset) return;        // plain refreshes coalesce; a reset always wins
  inFlight = true;
  const gen = ++generation;
  if (reset) publish({ status: null, balanceUsd: null, ready: false });
  let status: KalshiStatus | null = null;
  const sess = await session().catch(() => null);
  if (gen !== generation) return;
  if (!sess || isOwnerEmail(sess.user?.email)) {
    // signed out, or the platform owner: the owner view, with no extra request
    inFlight = false;
    publish({ status: sess ? { owner: true, connected: false } : null, balanceUsd: null, ready: true });
    return;
  }
  try {
    status = await kalshiUser.status();
  } catch {
    status = null;                       // server down / timeout -> owner view
  }
  if (gen !== generation) return;        // superseded by a newer session/refresh
  inFlight = false;
  publish({ status, balanceUsd: null, ready: true });
  if (status && status.connected && !status.owner) {
    const b = await kalshiUser.balance().catch(() => null);
    if (gen !== generation) return;
    const balanceUsd = b && b.source === 'user' && typeof b.balance_usd === 'number' ? b.balance_usd : null;
    publish({ status, balanceUsd, ready: true });
  }
}

// Reset only when the signed-in USER changes (sign-in / sign-out / switch);
// hourly TOKEN_REFRESHED events for the same user just refresh quietly.
let lastUserId: string | null | undefined = undefined;
supabase?.auth.onAuthStateChange((_event, session) => {
  const uid = session?.user?.id ?? null;
  const changed = uid !== lastUserId;
  lastUserId = uid;
  void refreshKalshiUser(changed);
});

export function useKalshiUser(): Store {
  const [s, setS] = useState<Store>(store);
  useEffect(() => {
    listeners.add(setS);
    setS(store);                          // catch an update that landed before subscribe
    if (!store.ready) void refreshKalshiUser();
    return () => { listeners.delete(setS); };
  }, []);
  return s;
}
