import React from 'react';
import { useTranslation } from 'react-i18next';

export default function LoadingState() {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-center justify-center h-full gap-3">
      <div className="flex gap-1.5">
        <div className="w-2 h-2 rounded-full bg-[var(--accent-primary)] animate-bounce [animation-delay:0ms]" />
        <div className="w-2 h-2 rounded-full bg-[var(--accent-primary)] animate-bounce [animation-delay:150ms]" />
        <div className="w-2 h-2 rounded-full bg-[var(--accent-primary)] animate-bounce [animation-delay:300ms]" />
      </div>
      <span className="text-xs text-[var(--text-muted)]">{t('common.loading')}</span>
    </div>
  );
}
