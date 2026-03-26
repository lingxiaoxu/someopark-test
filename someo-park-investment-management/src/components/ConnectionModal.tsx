import React, { useState } from 'react';
import { X, CheckCircle2, Terminal, ShieldAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export default function ConnectionModal({ onClose, onConnect }: { onClose: () => void, onConnect: () => void }) {
  const { t } = useTranslation();
  const [step, setStep] = useState(1);

  return (
    <div className="fixed inset-0 bg-black/60  flex items-center justify-center z-50 p-4">
      <div className="w-full max-w-md overflow-hidden flex flex-col" style={{ background: '#fff', border: '2px solid #111', boxShadow: 'var(--shadow-pixel-lg)' }}>
        
        {/* Header */}
        <div className="flex justify-between items-center p-5 border-b-2 border-b-black">
          <div className="flex items-center gap-2">
            <Terminal className="w-5 h-5 text-black" />
            <h2 className="text-base font-semibold text-black">{t('connection.title')}</h2>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-black transition-colors cursor-pointer">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 flex-1">
          {/* Progress Bar */}
          <div className="flex gap-2 mb-8">
            {[1, 2, 3].map((i) => (
              <div key={i} className={`h-2 flex-1 ${step >= i ? 'bg-black' : 'bg-[#e5e5e5]'}`} />
            ))}
          </div>

          {step === 1 && (
            <div className="flex flex-col gap-5 animate-in fade-in slide-in-from-bottom-4 duration-300">
              <div>
                <h3 className="text-lg font-medium text-black mb-1">{t('connection.agentConfig')}</h3>
                <p className="text-sm text-gray-500">{t('connection.agentConfigDesc')}</p>
              </div>
              
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1.5">{t('connection.agentName')}</label>
                  <input type="text" className="w-full bg-[#e5e5e5] border-2 border-black rounded-none px-3 py-2 text-sm text-black focus:outline-none focus:border-[var(--accent-primary)] transition-colors" defaultValue="Ling Quant Agent" />
                </div>
                
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1.5">{t('connection.operationMode')}</label>
                  <select className="w-full bg-[#e5e5e5] border-2 border-black rounded-none px-3 py-2 text-sm text-black focus:outline-none focus:border-[var(--accent-primary)] transition-colors appearance-none">
                    <option>{t('connection.modeResearch')}</option>
                    <option>{t('connection.modeDryRun')}</option>
                    <option>{t('connection.modePaper')}</option>
                  </select>
                </div>

                <div className="bg-[#fff8e5] border border-[#ffd699] rounded-none p-3 flex gap-3 mt-2">
                  <ShieldAlert className="w-5 h-5 text-[#ff6600] shrink-0" />
                  <p className="text-xs text-[#ff6600] leading-relaxed">
                    {t('connection.prodWarning')}
                  </p>
                </div>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="flex flex-col gap-5 animate-in fade-in slide-in-from-bottom-4 duration-300">
              <div>
                <h3 className="text-lg font-medium text-black mb-1">{t('connection.connectionDetails')}</h3>
                <p className="text-sm text-gray-500">{t('connection.connectionDetailsDesc')}</p>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1.5">{t('connection.agentToken')}</label>
                  <div className="code-block flex justify-between items-center">
                    <span className="text-black">oc_live_8f92a1b...</span>
                    <button className="text-xs text-gray-500 hover:text-black cursor-pointer">{t('common.copy')}</button>
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1.5">{t('connection.bridgeUrl')}</label>
                  <div className="code-block flex justify-between items-center">
                    <span className="text-black">wss://someopark.com/ws/someoclaw</span>
                    <button className="text-xs text-gray-500 hover:text-black cursor-pointer">{t('common.copy')}</button>
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1.5">{t('connection.workspaceId')}</label>
                  <div className="code-block flex justify-between items-center">
                    <span className="text-black">sp_user_001</span>
                    <button className="text-xs text-gray-500 hover:text-black cursor-pointer">{t('common.copy')}</button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="flex flex-col items-center justify-center gap-4 py-8 animate-in fade-in slide-in-from-bottom-4 duration-300">
              <div className="w-16 h-16 flex items-center justify-center mb-2" style={{ background: '#00cc66', border: '2px solid #111', boxShadow: 'var(--shadow-pixel)' }}>
                <CheckCircle2 className="w-8 h-8" style={{ color: '#fff' }} />
              </div>
              <h3 className="text-xl font-medium text-black">{t('connection.connectionVerified')}</h3>
              <p className="text-sm text-gray-500 text-center max-w-[280px]">
                {t('connection.connectionVerifiedDesc')}
              </p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-5 border-t-2 border-t-black bg-white flex justify-between items-center">
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
