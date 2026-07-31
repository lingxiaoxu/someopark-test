/**
 * MacroRightPanel — right-panel shell for `macro_*` artifacts. Mirrors the
 * existing RightPanel chrome (sticky header: title + type badge + maximize/close)
 * but lives entirely inside the macro family, so RightPanel.tsx (stock+prediction)
 * stays untouched. App.tsx routes macro_* artifacts here.
 */
import { useTranslation } from 'react-i18next';
import { X, Maximize2, Minimize2 } from 'lucide-react';
import MacroArtifact from './MacroArtifact';
import { MACRO_ITEMS } from './MacroArtifactGrid';

const KEY_BY_TYPE: Record<string, string> = Object.fromEntries(MACRO_ITEMS.map((i) => [i.type, i.i18nKey]));

export default function MacroRightPanel({ artifact, onClose, onMaximize, isMaximized }: {
  artifact: any; onClose: () => void; onMaximize?: () => void; isMaximized?: boolean;
}) {
  const { t } = useTranslation();
  const key = KEY_BY_TYPE[artifact.type];
  const displayTitle = key ? t(`macro.${key}`) : (artifact.title || artifact.type);
  const MaxIcon = isMaximized ? Minimize2 : Maximize2;

  return (
    <div className="h-full flex flex-col relative" style={{ background: 'var(--bg-tertiary)', borderLeft: '4px solid var(--ink)' }}>
      {/* Header — same sticky header style as RightPanel */}
      <div
        className="h-14 flex items-center justify-between px-4 shrink-0"
        style={{ background: 'var(--paper)', borderBottom: '3px solid var(--ink)' }}
      >
        <div className="flex items-center gap-3 min-w-0">
          <span className="truncate" style={{ fontFamily: 'var(--font-mono)', fontSize: '13px', fontWeight: 700, color: 'var(--ink)', textTransform: 'uppercase', letterSpacing: '.04em' }}>
            {displayTitle}
          </span>
          <span className="shrink-0" style={{
            padding: '2px 8px',
            fontFamily: 'var(--font-mono)',
            fontSize: '10px',
            fontWeight: 700,
            letterSpacing: '.08em',
            textTransform: 'uppercase',
            background: 'var(--ink)',
            color: 'var(--paper)',
            border: '2px solid var(--ink)',
          }}>
            {artifact.type}
          </span>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          {[
            { icon: MaxIcon, action: onMaximize || (() => {}) },
            { icon: X, action: onClose },
          ].map(({ icon: Icon, action }, i) => (
            <button
              key={i}
              onClick={action}
              style={{
                padding: '5px',
                background: 'transparent',
                border: '2px solid transparent',
                cursor: 'pointer',
                transition: 'all .1s',
                color: 'var(--ink-dim)',
              }}
              onMouseEnter={e => {
                const el = e.currentTarget as HTMLElement;
                el.style.background = 'var(--ink)';
                el.style.borderColor = 'var(--ink)';
                el.style.color = 'var(--paper)';
              }}
              onMouseLeave={e => {
                const el = e.currentTarget as HTMLElement;
                el.style.background = 'transparent';
                el.style.borderColor = 'transparent';
                el.style.color = 'var(--ink-dim)';
              }}
            >
              <Icon className="w-4 h-4" />
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        <MacroArtifact artifact={artifact} />
      </div>
    </div>
  );
}
