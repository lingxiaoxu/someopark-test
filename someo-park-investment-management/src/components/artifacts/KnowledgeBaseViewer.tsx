import React, { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';

interface KBResult {
  doc_id?: string;
  title?: string;
  section?: string;
  excerpt?: string;
  source_file?: string;
  score?: number;
  metadata?: Record<string, string | number>;
}

interface KnowledgeBaseData {
  query?: string;
  results?: KBResult[];
  total_results?: number;
  search_time_ms?: number;
  error?: string;
}

function ScoreBadge({ score }: { score?: number }) {
  if (score == null) return null;
  const color = score >= 0.8 ? 'var(--success)' : score >= 0.5 ? 'var(--warning)' : 'var(--text-muted)';
  return (
    <span className="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold" style={{ backgroundColor: `${color}15`, color }}>
      {score.toFixed(3)}
    </span>
  );
}

function ResultCard({ result, idx }: { result: KBResult; idx: number; key?: React.Key }) {
  const [expanded, setExpanded] = useState(false);
  const excerpt = result.excerpt || '';
  const isLong = excerpt.length > 200;
  const displayExcerpt = expanded || !isLong ? excerpt : excerpt.slice(0, 200) + '...';

  return (
    <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden hover:border-[var(--accent-primary)]/30 transition-colors">
      {/* Card Header */}
      <div
        className="flex items-start justify-between px-4 py-3 cursor-pointer hover:bg-[var(--bg-secondary)] transition-colors"
        onClick={() => isLong && setExpanded(!expanded)}
      >
        <div className="flex items-start gap-3 min-w-0 flex-1">
          {/* Index / Doc ID badge */}
          <div className="shrink-0 flex flex-col items-center gap-1">
            <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-[var(--accent-primary)]/10 text-[var(--accent-primary)]">
              {result.doc_id || `#${idx + 1}`}
            </span>
            <ScoreBadge score={result.score} />
          </div>

          {/* Title + Section */}
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-[var(--text-primary)] truncate">
              {result.title || 'Untitled'}
            </div>
            {result.section && (
              <div className="text-[10px] text-[var(--text-muted)] mt-0.5 flex items-center gap-1">
                <span className="uppercase tracking-wider">Section:</span>
                <span className="font-mono text-[var(--text-secondary)]">{result.section}</span>
              </div>
            )}
          </div>
        </div>

        {/* Expand toggle */}
        {isLong && (
          <div className="shrink-0 ml-2 mt-1">
            {expanded
              ? <ChevronDown className="w-3.5 h-3.5 text-[var(--text-muted)]" />
              : <ChevronRight className="w-3.5 h-3.5 text-[var(--text-muted)]" />
            }
          </div>
        )}
      </div>

      {/* Excerpt */}
      <div className="px-4 pb-3">
        <div className={`text-xs text-[var(--text-secondary)] leading-relaxed whitespace-pre-wrap ${expanded ? '' : 'line-clamp-4'}`}>
          {displayExcerpt}
        </div>

        {/* Metadata */}
        {result.metadata && Object.keys(result.metadata).length > 0 && expanded && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {Object.entries(result.metadata).map(([key, val]) => (
              <span key={key} className="px-1.5 py-0.5 rounded text-[10px] bg-[var(--bg-tertiary)] text-[var(--text-muted)] font-mono">
                {key}: {String(val)}
              </span>
            ))}
          </div>
        )}

        {/* Source file */}
        {result.source_file && (
          <div className="mt-2 pt-2 border-t border-[var(--border-subtle)]">
            <span className="text-[10px] text-[var(--text-muted)] flex items-center gap-1">
              <span className="uppercase tracking-wider">Source:</span>
              <span className="font-mono text-[var(--text-secondary)]">{result.source_file}</span>
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

export default function KnowledgeBaseViewer({ data }: { data?: KnowledgeBaseData; params?: any }) {
  if (!data) {
    return <div className="text-sm text-[var(--text-muted)] text-center py-8">No knowledge base results available.</div>;
  }

  if (data.error) {
    return (
      <div className="bg-[var(--error)]/10 border border-[var(--error)]/20 rounded-lg p-4">
        <div className="text-sm font-medium text-[var(--error)]">Error</div>
        <div className="text-xs text-[var(--text-secondary)] mt-1">{data.error}</div>
      </div>
    );
  }

  const results = data.results || [];

  return (
    <div className="flex flex-col h-full space-y-4">
      {/* Header */}
      <div className="shrink-0">
        {data.query && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg px-4 py-3 mb-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">Search Query</div>
            <div className="text-sm text-[var(--text-primary)] font-medium">"{data.query}"</div>
          </div>
        )}
        <div className="flex items-center justify-between">
          <div className="text-xs text-[var(--text-muted)]">
            {data.total_results != null ? data.total_results : results.length} result{results.length !== 1 ? 's' : ''} found
          </div>
          {data.search_time_ms != null && (
            <div className="text-[10px] text-[var(--text-muted)] font-mono">{data.search_time_ms}ms</div>
          )}
        </div>
      </div>

      {/* Results */}
      <div className="flex-1 overflow-y-auto space-y-3">
        {results.map((result, idx) => (
          <ResultCard key={result.doc_id || idx} result={result} idx={idx} />
        ))}
        {results.length === 0 && (
          <div className="text-sm text-[var(--text-muted)] text-center py-8">
            No results found{data.query ? ` for "${data.query}"` : ''}.
          </div>
        )}
      </div>
    </div>
  );
}
