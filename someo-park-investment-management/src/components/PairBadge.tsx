import React, { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ArrowRightLeft, TrendingUp, TrendingDown, ExternalLink } from 'lucide-react';
import { useSetArtifact } from '../contexts/ArtifactContext';

export interface PairBadgeProps {
  /** Full pair key like "MSFT/AAPL" or separate s1/s2 */
  pair?: string;
  s1?: string;
  s2?: string;
  /** "long" = s1 long, s2 short. "short" = s1 short, s2 long. null/undefined = neutral */
  direction?: 'long' | 'short' | null;
  /** Strategy for navigation: mrpt or mtfs */
  strategy?: string;
  /** Optional detail fields shown in popover */
  details?: {
    openDate?: string;
    daysHeld?: number;
    s1Shares?: number;
    s2Shares?: number;
    s1Price?: number;
    s2Price?: number;
    hedgeRatio?: number;
    paramSet?: string;
    zScore?: number;
    momentumSpread?: number;
    unrealizedPnl?: number;
    unrealizedPnlPct?: number;
  };
  /** Compact mode for table cells */
  compact?: boolean;
  /** Disable click popover (used inside popover itself) */
  noPopover?: boolean;
}

function fmtNum(v: number | null | undefined, decimals = 2): string {
  if (v == null || isNaN(v)) return '—';
  return v.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export default function PairBadge({ pair, s1, s2, direction, strategy, details, compact, noPopover }: PairBadgeProps) {
  const { t } = useTranslation();
  const [showPopover, setShowPopover] = useState(false);
  const [popoverStyle, setPopoverStyle] = useState<React.CSSProperties>({});
  const badgeRef = useRef<HTMLDivElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const setActiveArtifact = useSetArtifact();

  // Parse pair key
  const ticker1 = s1 || pair?.split('/')[0] || '?';
  const ticker2 = s2 || pair?.split('/')[1] || '?';

  // Direction-based colors
  const s1Long = direction === 'long';
  const hasDirection = direction === 'long' || direction === 'short';

  // Close popover on outside click
  useEffect(() => {
    if (!showPopover) return;
    const handler = (e: MouseEvent) => {
      if (badgeRef.current?.contains(e.target as Node)) return;
      if (popoverRef.current?.contains(e.target as Node)) return;
      setShowPopover(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showPopover]);

  // Close popover on scroll or resize (position would be stale)
  useEffect(() => {
    if (!showPopover) return;
    const close = () => setShowPopover(false);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [showPopover]);

  const handleClick = (e: React.MouseEvent) => {
    if (noPopover) return;
    e.stopPropagation();
    if (!showPopover && badgeRef.current) {
      const rect = badgeRef.current.getBoundingClientRect();
      const spaceRight = window.innerWidth - rect.left;
      const alignRight = spaceRight < 340;
      setPopoverStyle({
        position: 'fixed',
        top: rect.bottom + 4,
        left: alignRight ? rect.right - 320 : rect.left,
        zIndex: 9999,
        width: 320,
        minWidth: 280,
      });
    }
    setShowPopover(!showPopover);
  };

  const navigateTo = (type: string, title: string, params?: any) => {
    setActiveArtifact({ type, title, params });
    setShowPopover(false);
  };

  const pairKey = `${ticker1}/${ticker2}`;
  const strat = strategy?.toLowerCase() || 'mrpt';

  return (
    <div className="relative inline-flex" ref={badgeRef}>
      {/* Badge */}
      <button
        onClick={handleClick}
        className={`
          inline-flex items-center gap-0 rounded-md border transition-all
          ${noPopover ? 'cursor-default' : 'cursor-pointer hover:border-[var(--accent-primary)]/60 hover:shadow-[0_0_0_1px_var(--accent-primary)]/20'}
          ${compact ? 'px-1.5 py-0.5' : 'px-2 py-1'}
          bg-[var(--bg-primary)] border-[var(--border-subtle)]
        `}
      >
        {/* S1 ticker */}
        <span className={`font-mono font-semibold ${compact ? 'text-[11px]' : 'text-xs'}`} style={{
          color: hasDirection ? (s1Long ? 'var(--success)' : 'var(--error)') : 'var(--text-primary)'
        }}>
          {ticker1}
        </span>

        {/* Direction arrow */}
        <span className={`mx-1 flex items-center ${compact ? 'text-[9px]' : 'text-[10px]'}`}>
          {hasDirection ? (
            <ArrowRightLeft className={`${compact ? 'w-2.5 h-2.5' : 'w-3 h-3'} text-[var(--text-muted)]`} />
          ) : (
            <span className="text-[var(--text-muted)]">/</span>
          )}
        </span>

        {/* S2 ticker */}
        <span className={`font-mono font-semibold ${compact ? 'text-[11px]' : 'text-xs'}`} style={{
          color: hasDirection ? (s1Long ? 'var(--error)' : 'var(--success)') : 'var(--text-primary)'
        }}>
          {ticker2}
        </span>

        {/* Direction micro-label */}
        {hasDirection && (
          <span className={`ml-1.5 flex items-center gap-0.5 ${compact ? 'text-[8px]' : 'text-[9px]'} font-bold uppercase tracking-wider rounded px-1 py-px`} style={{
            color: direction === 'long' ? 'var(--success)' : 'var(--error)',
            backgroundColor: direction === 'long' ? 'color-mix(in srgb, var(--success) 12%, transparent)' : 'color-mix(in srgb, var(--error) 12%, transparent)',
          }}>
            {direction === 'long' ? (
              <TrendingUp className={compact ? 'w-2 h-2' : 'w-2.5 h-2.5'} />
            ) : (
              <TrendingDown className={compact ? 'w-2 h-2' : 'w-2.5 h-2.5'} />
            )}
            {!compact && (direction === 'long' ? t('common.long') : t('common.short'))}
          </span>
        )}
      </button>

      {/* Popover via Portal — avoids overflow clipping from parent tables */}
      {showPopover && !noPopover && createPortal(
        <div
          ref={popoverRef}
          className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg shadow-xl shadow-black/30 overflow-hidden"
          style={popoverStyle}
        >
          {/* Popover Header */}
          <div className="px-3 py-2.5 bg-[var(--bg-secondary)] border-b border-[var(--border-subtle)] flex items-center justify-between">
            <div className="flex items-center gap-2">
              <PairBadge pair={pairKey} direction={direction} compact noPopover />
              {strategy && <span className="text-[10px] font-mono text-[var(--text-muted)] uppercase">{strategy}</span>}
            </div>
            {details?.paramSet && (
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]">
                {details.paramSet}
              </span>
            )}
          </div>

          {/* Position Details */}
          {details && (
            <div className="px-3 py-2.5 space-y-2 border-b border-[var(--border-subtle)]">
              {/* Position Roles */}
              <div className="grid grid-cols-2 gap-2 text-[10px]">
                <div className="bg-[var(--bg-secondary)] rounded px-2 py-1.5">
                  <div className="text-[var(--text-muted)] uppercase mb-0.5">
                    {ticker1} <span style={{ color: hasDirection ? (s1Long ? 'var(--success)' : 'var(--error)') : 'var(--text-muted)' }}>
                      {hasDirection ? (s1Long ? t('common.long') : t('common.short')) : '—'}
                    </span>
                  </div>
                  <div className="font-mono text-[var(--text-primary)]">
                    {details.s1Shares != null ? `${details.s1Shares.toLocaleString()} ${t('common.shares')}` : '—'}
                  </div>
                  {details.s1Price != null && (
                    <div className="font-mono text-[var(--text-muted)]">@ ${fmtNum(details.s1Price)}</div>
                  )}
                </div>
                <div className="bg-[var(--bg-secondary)] rounded px-2 py-1.5">
                  <div className="text-[var(--text-muted)] uppercase mb-0.5">
                    {ticker2} <span style={{ color: hasDirection ? (s1Long ? 'var(--error)' : 'var(--success)') : 'var(--text-muted)' }}>
                      {hasDirection ? (s1Long ? t('common.short') : t('common.long')) : '—'}
                    </span>
                  </div>
                  <div className="font-mono text-[var(--text-primary)]">
                    {details.s2Shares != null ? `${details.s2Shares.toLocaleString()} ${t('common.shares')}` : '—'}
                  </div>
                  {details.s2Price != null && (
                    <div className="font-mono text-[var(--text-muted)]">@ ${fmtNum(details.s2Price)}</div>
                  )}
                </div>
              </div>

              {/* Key Metrics Row */}
              <div className="flex items-center gap-3 text-[10px]">
                {details.hedgeRatio != null && (
                  <div>
                    <span className="text-[var(--text-muted)]">HR </span>
                    <span className="font-mono text-[var(--text-primary)]">{fmtNum(details.hedgeRatio, 3)}</span>
                  </div>
                )}
                {details.zScore != null && (
                  <div>
                    <span className="text-[var(--text-muted)]">Z </span>
                    <span className="font-mono text-[var(--text-primary)]">{fmtNum(details.zScore, 3)}</span>
                  </div>
                )}
                {details.momentumSpread != null && (
                  <div>
                    <span className="text-[var(--text-muted)]">Mom </span>
                    <span className="font-mono text-[var(--text-primary)]">{fmtNum(details.momentumSpread, 3)}</span>
                  </div>
                )}
                {details.openDate && (
                  <div>
                    <span className="text-[var(--text-muted)]">{t('pairBadge.entry')} </span>
                    <span className="font-mono text-[var(--text-primary)]">{details.openDate}</span>
                  </div>
                )}
                {details.daysHeld != null && (
                  <div>
                    <span className="text-[var(--text-muted)]">{t('pairBadge.held')} </span>
                    <span className="font-mono text-[var(--text-primary)]">{details.daysHeld}d</span>
                  </div>
                )}
              </div>

              {/* PnL Row */}
              {details.unrealizedPnl != null && (
                <div className="flex items-center gap-2 text-[10px]">
                  <span className="text-[var(--text-muted)]">{t('pairBadge.unrealizedPnl')}</span>
                  <span className="font-mono font-semibold" style={{ color: details.unrealizedPnl >= 0 ? 'var(--success)' : 'var(--error)' }}>
                    {details.unrealizedPnl >= 0 ? '+' : ''}${fmtNum(details.unrealizedPnl)}
                  </span>
                  {details.unrealizedPnlPct != null && (
                    <span className="font-mono" style={{ color: details.unrealizedPnlPct >= 0 ? 'var(--success)' : 'var(--error)' }}>
                      ({details.unrealizedPnlPct >= 0 ? '+' : ''}{fmtNum(details.unrealizedPnlPct)}%)
                    </span>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Quick Links */}
          <div className="px-3 py-2 space-y-1">
            <div className="text-[9px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('pairBadge.quickLinks')}</div>
            <button
              onClick={() => navigateTo('inventory', `Inventory (${strat.toUpperCase()})`, { strategy: strat })}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-[11px] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] transition-colors text-left"
            >
              <ExternalLink className="w-3 h-3 text-[var(--text-muted)]" />
              {t('pairBadge.currentInventory')}
            </button>
            <button
              onClick={() => navigateTo('oos_pair_summary', `OOS Pair Summary`, { strategy: strat })}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-[11px] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] transition-colors text-left"
            >
              <ExternalLink className="w-3 h-3 text-[var(--text-muted)]" />
              {t('pairBadge.oosPairPerformance')}
            </button>
            <button
              onClick={() => navigateTo('wf_grid', `DSR Selection Log`, { strategy: strat })}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-[11px] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] transition-colors text-left"
            >
              <ExternalLink className="w-3 h-3 text-[var(--text-muted)]" />
              {t('pairBadge.dsrSelectionGrid')}
            </button>
            <button
              onClick={() => navigateTo('inventory_history', `Inventory History`, { strategy: strat })}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-[11px] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] transition-colors text-left"
            >
              <ExternalLink className="w-3 h-3 text-[var(--text-muted)]" />
              {t('pairBadge.tradeHistory')}
            </button>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}

/**
 * Utility to parse a "S1/S2" pair key string into s1 and s2.
 */
export function parsePairKey(pairKey: string): { s1: string; s2: string } {
  const parts = pairKey.split('/');
  return { s1: parts[0] || '?', s2: parts[1] || '?' };
}
