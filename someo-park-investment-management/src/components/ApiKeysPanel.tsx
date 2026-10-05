import { useEffect, useRef, useState } from 'react';
import { KeyRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  kalshiUser, refreshKalshiUser, checkKeyIdLocal, checkPemFileLocal, MIRROR_RATIOS, type KalshiStatus,
} from '../lib/kalshiUser';

// Same shell, spacing, fonts and button styling as the sidebar Settings panel
// (Sidebar.tsx, showCardSettings branch) — opened in place inside the menu.
const mono = { fontFamily: 'var(--font-mono)' } as const;
const label = { ...mono, fontSize: '10px', fontWeight: 700, color: '#111', letterSpacing: '.04em', marginBottom: 4 } as const;
const hint = { ...mono, fontSize: '10px', color: '#555', lineHeight: 1.6 } as const;
const btn = (active = false, disabled = false) => ({
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  padding: '6px 10px', ...mono, fontSize: '11px', fontWeight: 700,
  border: '1px solid #e5e5e5', cursor: disabled ? 'not-allowed' : 'pointer',
  background: active ? '#111' : '#f9f9f9', color: active ? '#fff' : '#333',
  opacity: disabled ? 0.5 : 1,
}) as const;

export default function ApiKeysPanel({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<KalshiStatus | null>(null);
  const [keyId, setKeyId] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [ratio, setRatio] = useState<number>(0.25);
  const [ack, setAck] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);

  const load = async () => {
    try { setStatus(await kalshiUser.status()); } catch (e: any) { setError(e.message); }
  };
  useEffect(() => { void load(); }, []);

  const run = async (what: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(what); setError(null); setNote(null);
    try { await fn(); if (ok) setNote(ok); await load(); await refreshKalshiUser(true); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(null); }
  };

  const saveKey = () => {
    const err = checkKeyIdLocal(keyId);
    if (err) { setError(err); setNote(null); return; }
    void run('key', () => kalshiUser.saveKeyId(keyId), t('apiKeys.keySaved', '已保存 API Key'))
      .then(() => setKeyId(''));
  };

  const uploadPem = () => {
    const files = fileRef.current?.files ?? null;
    const err = checkPemFileLocal(files);
    if (err) { setError(err); setNote(null); return; }
    void run('pem', () => kalshiUser.uploadPem(files![0]), t('apiKeys.pemSaved', '私钥文件已上传'))
      .then(() => { if (fileRef.current) fileRef.current.value = ''; });
  };

  const verify = () => void run('verify', async () => {
    const r = await kalshiUser.verify();
    setNote(t('apiKeys.verified', '检测通过，已连接你的 Kalshi Production 账户') + `（$${r.balance_usd.toFixed(2)}）`);
  });

  const disconnect = () => void run('disconnect', () => kalshiUser.disconnect(), t('apiKeys.disconnected', '已断开，平台恢复显示默认账户'));

  // Live-trading application (2026-10-04): the user applies and can stop here;
  // the platform owner reviews and enables it by hand.
  const pct = (r?: number | null) => (typeof r === 'number' ? `${Math.round(r * 100)}%` : '—');
  const applyTrading = () => void run('trading', () => kalshiUser.requestTrading(ratio),
    t('apiKeys.tradingApplied', '已提交申请，平台审核后开通')).then(() => setAck(false));
  const cancelTrading = () => void run('trading', () => kalshiUser.cancelTradingRequest(),
    t('apiKeys.tradingCancelled', '已撤回申请'));
  const stopTrading = () => {
    if (!confirmStop) { setConfirmStop(true); window.setTimeout(() => setConfirmStop(false), 5000); return; }
    setConfirmStop(false);
    void run('trading', () => kalshiUser.stopTrading(), t('apiKeys.tradingStoppedNote', '已停止跟单，从下一笔起不再下单'));
  };
  const tr = status?.trading ?? null;
  const trState = tr?.state ?? 'none';
  const trText = trState === 'requested' ? t('apiKeys.tradingStateRequested', '已申请（比例 {{ratio}}），等待平台审核', { ratio: pct(tr?.requested_ratio) })
    : trState === 'approved' ? t('apiKeys.tradingStateApproved', '已批准（比例 {{ratio}}），将在下一个交易间隙生效', { ratio: pct(tr?.approved_ratio) })
    : trState === 'active' ? t('apiKeys.tradingStateActive', '已开通（比例 {{ratio}}）', { ratio: pct(tr?.approved_ratio) })
    : trState === 'stopped' ? (tr?.stopped_by === 'owner' ? t('apiKeys.tradingStateStoppedOwner', '已由平台停止') : t('apiKeys.tradingStateStopped', '已停止'))
    : t('apiKeys.tradingStateNone', '未开通');
  const trColor = trState === 'active' ? '#166534' : trState === 'stopped' ? '#b91c1c' : '#555';

  const hasKey = !!(status?.pending_key_id || status?.key_id_masked);
  const canVerify = hasKey && !!status?.pem_uploaded && !busy;

  return (
    <>
      <div style={{ padding: '8px 12px', borderBottom: '2px solid #111', background: '#111', color: '#fff', display: 'flex', alignItems: 'center', gap: 6 }}>
        <button onClick={onBack} style={{ background: 'none', border: 'none', color: '#fff', cursor: 'pointer', fontSize: '12px', padding: 0 }}>←</button>
        <KeyRound style={{ width: 12, height: 12 }} />
        <span style={{ fontSize: '10px', ...mono, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase' }}>{t('sidebar.apiKeysTitle', 'API Keys')}</span>
      </div>
      <div style={{ padding: '12px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        {status?.owner ? (
          <div style={hint}>{t('apiKeys.ownerNote', '此账户使用平台主账户的 Kalshi Production 密钥，无需填写。')}</div>
        ) : (
          <>
            <div style={hint}>{t('apiKeys.desc', '连接你自己的 Kalshi Production 账户。两项都上传并通过检测后，平台将显示你自己账户的数据；实盘跟单需要你在下方申请，并由平台人工审核开通。')}</div>
            {status?.user_id && (
              <div style={{ ...hint, wordBreak: 'break-all' }}>
                {t('apiKeys.account', '账户')}: {status.email}<br />ID: {status.user_id}
              </div>
            )}

            <div>
              <div style={label}>Kalshi Production API Key</div>
              {status?.key_id_masked && (
                <div style={{ ...hint, marginBottom: 2 }}>{t('apiKeys.current', '当前')}: {status.key_id_masked}</div>
              )}
              {status?.pending_key_id_masked && (
                <div style={{ ...hint, marginBottom: 4 }}>
                  {t('apiKeys.pending', '待检测')}: {status.pending_key_id_masked}
                </div>
              )}
              <div style={{ display: 'flex', gap: 6 }}>
                <input
                  value={keyId}
                  onChange={e => setKeyId(e.target.value)}
                  placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                  spellCheck={false} autoComplete="off"
                  style={{ flex: 1, minWidth: 0, padding: '6px 8px', ...mono, fontSize: '10px', border: '1px solid #e5e5e5', background: '#fff', color: '#111' }}
                />
                <button onClick={saveKey} disabled={!!busy || !keyId} style={btn(false, !!busy || !keyId)}>
                  {busy === 'key' ? '…' : t('apiKeys.save', '保存')}
                </button>
              </div>
            </div>

            <div>
              <div style={label}>Kalshi Production Private Key File</div>
              {status?.pem_uploaded && (
                <div style={{ ...hint, marginBottom: 4 }}>
                  {status.pending_pem
                    ? t('apiKeys.pemPending', '新私钥文件已上传，检测通过后替换当前私钥')
                    : t('apiKeys.pemPresent', '已上传私钥文件')}
                </div>
              )}
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input ref={fileRef} type="file" accept=".pem,.txt,.key,text/plain" multiple={false}
                  style={{ flex: 1, minWidth: 0, ...mono, fontSize: '10px', color: '#333' }} />
                <button onClick={uploadPem} disabled={!!busy} style={btn(false, !!busy)}>
                  {busy === 'pem' ? '…' : t('apiKeys.upload', '上传')}
                </button>
              </div>
              <div style={{ ...hint, marginTop: 2 }}>{t('apiKeys.pemRule', '只接受单个私钥文件（Kalshi 下载的 .txt 或 .pem，Ed25519 或 RSA），不超过 4 KB')}</div>
            </div>

            <div style={{ borderTop: '1px solid #e5e5e5', paddingTop: 8 }}>
              <div style={{ ...hint, marginBottom: 6 }}>
                {t('apiKeys.status', '状态')}:{' '}
                <b style={{ color: status?.connected ? '#166534' : status?.auth_failed ? '#b91c1c' : '#555' }}>
                  {status?.connected ? t('apiKeys.connected', '已连接')
                    : status?.auth_failed ? t('apiKeys.authFailed', '认证失效，请重新检测')
                    : t('apiKeys.notConnected', '未连接（平台显示默认账户）')}
                </b>
              </div>
              <div style={{ display: 'flex', gap: 6 }}>
                <button onClick={verify} disabled={!canVerify} style={{ ...btn(true, !canVerify), flex: 1 }}>
                  {busy === 'verify' ? t('apiKeys.verifying', '检测中…') : t('apiKeys.verify', '检测并连接')}
                </button>
                {(status?.connected || status?.auth_failed || status?.pem_uploaded || hasKey) && (
                  <button onClick={disconnect} disabled={!!busy} style={btn(false, !!busy)}>
                    {t('apiKeys.remove', '删除')}
                  </button>
                )}
              </div>
            </div>

            {status?.connected && (
              <div style={{ borderTop: '1px solid #e5e5e5', paddingTop: 8 }}>
                <div style={label}>{t('apiKeys.trading', '实盘跟单')}</div>
                <div style={{ ...hint, marginBottom: 6 }}>
                  {t('apiKeys.status', '状态')}: <b style={{ color: trColor }}>{trText}</b>
                </div>
                {(trState === 'none' || trState === 'stopped') && (
                  <>
                    <div style={{ ...hint, marginBottom: 8 }}>
                      {t('apiKeys.tradingRisk', '实盘跟单会用你 Kalshi 账户里的真钱自动下单：平台策略每下一笔，你的账户按你选的比例跟一笔（向下取整，不足 1 张跳过）。策略可能连续亏损，你可能亏掉全部本金；过往表现不代表未来。平台主账户总是先下单，你的成交价可能更差、成交可能更少。申请后由平台人工审核开通；你可以随时在这里停止，下一笔起生效。')}
                    </div>
                    <div style={label}>{t('apiKeys.tradingRatio', '跟单比例')}</div>
                    <div style={{ display: 'flex', gap: 6, marginBottom: 4 }}>
                      {MIRROR_RATIOS.map(r => (
                        <button key={r} onClick={() => setRatio(r)} disabled={!!busy}
                          style={{ ...btn(ratio === r, !!busy), flex: 1 }}>{pct(r)}</button>
                      ))}
                    </div>
                    <div style={{ ...hint, marginBottom: 8 }}>
                      {t('apiKeys.tradingRatioHint', '按 100% 跟单，一个 15 分钟窗口可能占用一两百美元；余额较小时建议选 10% 或 25%。')}
                    </div>
                    <label style={{ ...hint, display: 'flex', gap: 6, alignItems: 'flex-start', marginBottom: 8, cursor: 'pointer' }}>
                      <input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} style={{ marginTop: 2 }} />
                      <span>{t('apiKeys.tradingAck', '我已阅读并理解上述风险，同意用我 Kalshi 账户里的真钱跟单。')}</span>
                    </label>
                    <button onClick={applyTrading} disabled={!ack || !!busy} style={{ ...btn(true, !ack || !!busy), width: '100%' }}>
                      {busy === 'trading' ? '…' : t('apiKeys.tradingApply', '申请开通')}
                    </button>
                  </>
                )}
                {trState === 'requested' && (
                  <button onClick={cancelTrading} disabled={!!busy} style={{ ...btn(false, !!busy), width: '100%' }}>
                    {busy === 'trading' ? '…' : t('apiKeys.tradingCancel', '撤回申请')}
                  </button>
                )}
                {(trState === 'active' || trState === 'approved') && (
                  <button onClick={stopTrading} disabled={!!busy}
                    style={{ ...btn(confirmStop, !!busy), width: '100%', ...(confirmStop ? { background: '#b91c1c', color: '#fff' } : {}) }}>
                    {busy === 'trading' ? '…' : confirmStop ? t('apiKeys.tradingStopConfirm', '再点一次确认停止') : t('apiKeys.tradingStop', '停止跟单')}
                  </button>
                )}
              </div>
            )}
          </>
        )}
        {error && <div role="alert" style={{ ...mono, fontSize: '10px', color: '#b91c1c', lineHeight: 1.5 }}>{error}</div>}
        {note && !error && <div role="status" style={{ ...mono, fontSize: '10px', color: '#166534', lineHeight: 1.5 }}>{note}</div>}
      </div>
    </>
  );
}
