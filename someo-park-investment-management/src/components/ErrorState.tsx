import React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, RefreshCw } from 'lucide-react';

export default function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-center px-6">
      <AlertCircle className="w-8 h-8 text-[var(--error)]" />
      <div className="text-sm text-[var(--text-primary)] font-medium">{t('common.failedToLoad')}</div>
      <div className="text-xs text-[var(--text-muted)] max-w-xs">{message}</div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-2 flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] border border-[var(--border-subtle)] transition-colors"
        >
          <RefreshCw className="w-3 h-3" />
          {t('common.retry')}
        </button>
      )}
    </div>
  );
}
