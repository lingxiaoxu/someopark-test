import React, { useState } from 'react';
import { X, CheckCircle2, Terminal, ShieldAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export default function ConnectionModal({ onClose, onConnect }: { onClose: () => void, onConnect: () => void }) {
  const { t } = useTranslation();
  const [step, setStep] = useState(1);

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-2xl w-full max-w-md shadow-2xl overflow-hidden flex flex-col">
        
        {/* Header */}
        <div className="flex justify-between items-center p-5 border-b border-[var(--border-subtle)]">
          <div className="flex items-center gap-2">
            <Terminal className="w-5 h-5 text-[var(--accent-primary)]" />
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{t('connection.title')}</h2>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors cursor-pointer">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 flex-1">
          {/* Progress Bar */}
          <div className="flex gap-2 mb-8">
            {[1, 2, 3].map((i) => (
              <div key={i} className={`h-1 flex-1 rounded-full ${step >= i ? 'bg-[var(--accent-primary)]' : 'bg-[var(--bg-tertiary)]'}`} />
            ))}
          </div>

          {step === 1 && (
            <div className="flex flex-col gap-5 animate-in fade-in slide-in-from-bottom-4 duration-300">
              <div>
                <h3 className="text-lg font-medium text-[var(--text-primary)] mb-1">{t('connection.agentConfig')}</h3>
                <p className="text-sm text-[var(--text-muted)]">{t('connection.agentConfigDesc')}</p>
              </div>
              
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">{t('connection.agentName')}</label>
                  <input type="text" className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-subtle)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)] transition-colors" defaultValue="Ling Quant Agent" />
                </div>
                
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">{t('connection.operationMode')}</label>
                  <select className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-subtle)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)] transition-colors appearance-none">
                    <option>{t('connection.modeResearch')}</option>
                    <option>{t('connection.modeDryRun')}</option>
                    <option>{t('connection.modePaper')}</option>
                  </select>
                </div>

                <div className="bg-[var(--warning)]/10 border border-[var(--warning)]/20 rounded-lg p-3 flex gap-3 mt-2">
                  <ShieldAlert className="w-5 h-5 text-[var(--warning)] shrink-0" />
                  <p className="text-xs text-[var(--warning)] leading-relaxed">
                    {t('connection.prodWarning')}
                  </p>
                </div>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="flex flex-col gap-5 animate-in fade-in slide-in-from-bottom-4 duration-300">
              <div>
                <h3 className="text-lg font-medium text-[var(--text-primary)] mb-1">{t('connection.connectionDetails')}</h3>
                <p className="text-sm text-[var(--text-muted)]">{t('connection.connectionDetailsDesc')}</p>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">{t('connection.agentToken')}</label>
                  <div className="code-block flex justify-between items-center">
                    <span className="text-[var(--accent-primary)]">oc_live_8f92a1b...</span>
                    <button className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer">{t('common.copy')}</button>
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">{t('connection.bridgeUrl')}</label>
                  <div className="code-block flex justify-between items-center">
                    <span className="text-[var(--text-primary)]">wss://someopark.com/ws/someoclaw</span>
                    <button className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer">{t('common.copy')}</button>
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">{t('connection.workspaceId')}</label>
                  <div className="code-block flex justify-between items-center">
                    <span className="text-[var(--text-primary)]">sp_user_001</span>
                    <button className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer">{t('common.copy')}</button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="flex flex-col items-center justify-center gap-4 py-8 animate-in fade-in slide-in-from-bottom-4 duration-300">
              <div className="w-16 h-16 rounded-full bg-[var(--success)]/10 flex items-center justify-center mb-2">
                <CheckCircle2 className="w-8 h-8 text-[var(--success)]" />
              </div>
              <h3 className="text-xl font-medium text-[var(--text-primary)]">{t('connection.connectionVerified')}</h3>
              <p className="text-sm text-[var(--text-muted)] text-center max-w-[280px]">
                {t('connection.connectionVerifiedDesc')}
              </p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-5 border-t border-[var(--border-subtle)] bg-[var(--bg-primary)] flex justify-between items-center">
          {step > 1 && step < 3 ? (
            <button className="button button-ghost" onClick={() => setStep(step - 1)}>{t('common.back')}</button>
          ) : <div></div>}
          
          {step < 3 ? (
            <button className="button button-primary" onClick={() => setStep(step + 1)}>
              {step === 1 ? t('connection.generateConfig') : t('connection.verifyConnection')}
            </button>
          ) : (
            <button className="button button-primary w-full" onClick={onConnect}>
              {t('connection.startUsing')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
