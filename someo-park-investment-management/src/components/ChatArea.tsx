import React, { useState } from 'react';
import { Send, Paperclip, Command, Activity, Terminal, Cloud, Laptop } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { InventoryBox, WFBox } from './FinancialComponents';
import { useApi } from '../hooks/useApi';
import { getInventory } from '../lib/api';
import PairBadge from './PairBadge';

export default function ChatArea({ agentMode, isLocalConnected, setActiveArtifact }: { agentMode: 'cloud' | 'local', isLocalConnected: boolean, setActiveArtifact: (a: any) => void }) {
  const { t } = useTranslation();
  const [input, setInput] = useState('');
  const { data: mrptInv } = useApi(() => getInventory('mrpt'), []);
  const activePairs = mrptInv ? Object.entries(mrptInv.pairs || {}).filter(([, p]: any) => (p as any).direction !== null) : [];

  return (
    <div className="flex flex-col h-full bg-[var(--bg-primary)] relative">
      {/* Header */}
      <div className="h-14 border-b border-[var(--border-subtle)] flex items-center justify-between px-6 shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-[var(--text-secondary)]">{t('chat.currentRuntime')}</span>
          {agentMode === 'cloud' ? (
            <div className="flex items-center gap-2 px-2.5 py-1 rounded-full bg-[var(--bg-tertiary)] border border-[var(--border-subtle)]">
              <Cloud className="w-3.5 h-3.5 text-[var(--accent-primary)]" />
              <span className="text-xs font-mono text-[var(--text-primary)]">{t('chat.cloudVpsLabel')}</span>
            </div>
          ) : (
            <div className="flex items-center gap-2 px-2.5 py-1 rounded-full bg-[var(--bg-tertiary)] border border-[var(--border-subtle)]">
              <Laptop className="w-3.5 h-3.5 text-[var(--success)]" />
              <span className="text-xs font-mono text-[var(--text-primary)]">{t('chat.localConnectedLabel')}</span>
            </div>
          )}
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-6 flex flex-col items-center">
        <div className="w-full max-w-3xl flex flex-col gap-8 pb-4">
          <div className="message-user">
            <p className="text-sm">{t('chat.userMsg1')}</p>
          </div>

          <div className="message-ai">
            <div className="w-8 h-8 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center shrink-0 border border-[var(--border-subtle)]">
              <Terminal className="w-4 h-4 text-[var(--accent-primary)]" />
            </div>
            <div className="message-content w-full">
              <div className="flex items-center gap-2 text-xs text-[var(--text-muted)] mb-1">
                <Activity className="w-3 h-3" />
                <span>{t('chat.runningTool', { mode: agentMode === 'cloud' ? t('chat.cloudVpsMode') : t('chat.localAgentMode') })}</span>
              </div>
              <p className="text-sm text-[var(--text-primary)] leading-relaxed">
                {t('chat.aiMsg1', { count: activePairs.length, date: mrptInv?.as_of || '...' })}
              </p>
              <div className="flex flex-wrap gap-3 mt-2">
                {activePairs.map(([key, pos]: any) => (
                  <span key={key}>
                    <PairBadge
                      pair={key}
                      direction={pos.direction}
                      strategy="mrpt"
                      details={{
                        s1Shares: pos.s1_shares,
                        s2Shares: pos.s2_shares,
                        s1Price: pos.open_s1_price,
                        s2Price: pos.open_s2_price,
                        openDate: pos.open_date,
                        hedgeRatio: pos.open_hedge_ratio,
                        paramSet: pos.param_set,
                        zScore: pos.open_signal?.z_score,
                      }}
                    />
                  </span>
                ))}
              </div>
            </div>
          </div>
          
          <div className="message-user">
            <p className="text-sm">{t('chat.userMsg2')}</p>
          </div>

          <div className="message-ai">
            <div className="w-8 h-8 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center shrink-0 border border-[var(--border-subtle)]">
              <Terminal className="w-4 h-4 text-[var(--accent-primary)]" />
            </div>
            <div className="message-content w-full">
              <p className="text-sm text-[var(--text-primary)] leading-relaxed">
                {t('chat.aiMsg2')}
              </p>
              <div className="mt-4 flex flex-col gap-4">
                
                <div className="space-y-2">
                  <div className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">{t('chat.section1')}</div>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => setActiveArtifact({ type: 'pair_universe', title: 'Pair Universe (MRPT/MTFS)', params: { strategy: 'mrpt' } })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnPairUniverse')}
                    </button>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">{t('chat.section2')}</div>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => setActiveArtifact({ type: 'wf_summary', title: 'walk_forward_summary.json', params: { strategy: 'mrpt' } })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnWfSummary')}
                    </button>
                    <button onClick={() => setActiveArtifact({ type: 'wf_grid', title: 'dsr_selection_log.csv', params: { strategy: 'mrpt' } })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnDsrGrid')}
                    </button>
                    <button onClick={() => setActiveArtifact({ type: 'chart', title: 'oos_equity_curve.csv', params: { strategy: 'mrpt' } })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnOosEquity')}
                    </button>
                    <button onClick={() => setActiveArtifact({ type: 'oos_pair_summary', title: 'oos_pair_summary.csv', params: { strategy: 'mrpt' } })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnOosPairSummary')}
                    </button>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">{t('chat.section3')}</div>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => setActiveArtifact({ type: 'table', title: 'mrpt_signals / mtfs_signals', params: { strategy: 'mrpt' } })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnSignals')}
                    </button>
                    <button onClick={() => setActiveArtifact({ type: 'daily_report', title: 'daily_report.json / .txt' })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnDailyReport')}
                    </button>
                    <button onClick={() => setActiveArtifact({ type: 'portfolio_history', title: 'monitor_history.xlsx' })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnMonitorHistory')}
                    </button>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">{t('chat.section4')}</div>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => setActiveArtifact({ type: 'inventory', title: 'inventory_mrpt.json', params: { strategy: 'mrpt' } })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnCurrentInventory')}
                    </button>
                    <button onClick={() => setActiveArtifact({ type: 'inventory_history', title: 'inventory_history/', params: { strategy: 'mrpt' } })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnInventoryHistory')}
                    </button>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">{t('chat.section5')}</div>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => setActiveArtifact({ type: 'wf_diagnostic', title: 'wf_diagnostic.xlsx' })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnWfDiagnostic')}
                    </button>
                    <button onClick={() => setActiveArtifact({ type: 'dashboard', title: 'Macro Regime Status' })} className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]">
                      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" /> {t('chat.btnRegime')}
                    </button>
                  </div>
                </div>

              </div>
            </div>
          </div>

          <div className="message-user">
            <p className="text-sm">{t('chat.userMsg3')}</p>
          </div>

          <div className="message-ai">
            <div className="w-8 h-8 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center shrink-0 border border-[var(--border-subtle)]">
              <Terminal className="w-4 h-4 text-[var(--accent-primary)]" />
            </div>
            <div className="message-content w-full">
              <p className="text-sm text-[var(--text-primary)] leading-relaxed">
                {t('chat.aiMsg3')}
              </p>
              <div className="mt-2">
                <WFBox windows={[
                  { sharpe: '1.2', pnl: '+4.5%' },
                  { sharpe: '0.8', pnl: '+2.1%' },
                  { sharpe: '-0.5', pnl: '-1.2%' },
                  { sharpe: '1.5', pnl: '+5.8%' },
                  { sharpe: '2.1', pnl: '+8.4%' },
                  { sharpe: '0.9', pnl: '+3.0%' }
                ]} />
              </div>
              <div className="mt-4">
                <button 
                  onClick={() => setActiveArtifact({ type: 'chart', title: 'CL/SRE OOS Equity Curve' })}
                  className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]"
                >
                  <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" />
                  {t('chat.btnEquityCurve')}
                </button>
              </div>
            </div>
          </div>
          
          <div className="message-user">
            <p className="text-sm">{t('chat.userMsg4')}</p>
          </div>

          <div className="message-ai">
            <div className="w-8 h-8 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center shrink-0 border border-[var(--border-subtle)]">
              <Terminal className="w-4 h-4 text-[var(--accent-primary)]" />
            </div>
            <div className="message-content w-full">
              <p className="text-sm text-[var(--text-primary)] leading-relaxed">
                {t('chat.aiMsg4')}
              </p>
              <div className="mt-4">
                <button 
                  onClick={() => setActiveArtifact({ type: 'wf_structure', title: 'Walk-Forward File Structure' })}
                  className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]"
                >
                  <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" />
                  {t('chat.btnWfStructure')}
                </button>
              </div>
            </div>
          </div>
          
          {/* Thinking state mock */}
          <div className="message-ai">
            <div className="w-8 h-8 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center shrink-0 border border-[var(--border-subtle)] shimmer">
              <Terminal className="w-4 h-4 text-[var(--text-muted)]" />
            </div>
            <div className="message-content justify-center">
              <span className="text-sm text-[var(--text-muted)] shimmer">{t('chat.agentThinking')}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Input Area */}
      <div className="p-6 pt-0 shrink-0 flex justify-center">
        <div className="w-full max-w-3xl">
          <div className="chat-input transition-colors">
            <button className="p-1.5 text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-secondary)] rounded-md transition-colors shrink-0">
              <Paperclip className="w-4 h-4" />
            </button>
            <textarea 
              className="chat-textarea mx-2" 
              placeholder={t('chat.inputPlaceholder', { mode: agentMode === 'cloud' ? t('chat.cloudVpsInput') : t('chat.localOpenClawInput') })}
              rows={1}
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
            <button className="p-1.5 text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-secondary)] rounded-md transition-colors shrink-0 mr-2">
              <Command className="w-4 h-4" />
            </button>
            <button className="button button-primary shrink-0 !h-8 !px-3">
              <Send className="w-3.5 h-3.5" />
            </button>
          </div>
          <div className="text-center mt-3">
            <span className="text-[10px] text-[var(--text-muted)]">
              {agentMode === 'cloud'
                ? t('chat.footerCloud')
                : t('chat.footerLocal')}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
