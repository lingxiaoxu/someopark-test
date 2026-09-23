import { useEffect, useRef, useState } from "react";
import CryptoArtifact, { ARTIFACTS } from "./Artifacts";
import { useCryptoPrediction } from "./CryptoContext";
import StrategyToggle from "./StrategyToggle";
import HealthIssues from "./HealthIssues";
import { date, Empty } from "./primitives";
import type { ArtifactType, StrategyId } from "./types";
import "./artifacts.css";

const PREFIX = "crypto_";

function artifactType(type: string): ArtifactType | undefined {
  if (!type.startsWith(PREFIX)) return undefined;
  return ARTIFACTS.find((item) => `${PREFIX}${item.type}` === type)?.type;
}

export function isCryptoArtifact(type: string): boolean {
  return typeof type === "string" && artifactType(type) !== undefined;
}

export function cryptoArtifactTitle(type: string): string {
  return ARTIFACTS.find((item) => `${PREFIX}${item.type}` === type)?.title ?? "加密货币预测市场";
}

// Only the artifact body lives here. The application owns the panel header,
// close/maximize controls, scrolling, and width in its existing RightPanel.
export default function CryptoPanelContent({ artifact }: { artifact: any }) {
  const { data, error, loading, refresh, selected, choose, now, stale, strategy } =
    useCryptoPrediction();
  const type = artifactType(String(artifact.type ?? ""));
  const [filter, setFilter] = useState<{
    strategyId: StrategyId;
    ticker?: string;
  }>({
    strategyId: selected,
    ticker: artifact.strategyId === selected && typeof artifact.ticker === "string" ? artifact.ticker : undefined,
  });

  const previousArtifact = useRef<unknown>(null);
  // Market cards use top-level scope; chat artifacts use the existing shared
  // params envelope. Apply an explicit strategy only on a NEW artifact, so a
  // later manual selector change remains the user's choice.
  useEffect(() => {
    if (previousArtifact.current !== artifact) {
      previousArtifact.current = artifact;
      const requested = artifact.strategyId ?? artifact.params?.strategyId;
      const target = requested === "fave" || requested === "pfme" ? requested : selected;
      const ticker = artifact.ticker ?? artifact.params?.ticker;
      if (target !== selected) choose(target);
      setFilter({ strategyId: target, ticker: typeof ticker === "string" ? ticker : undefined });
      return;
    }
    // A manual strategy change clears the old contract rather than carrying it.
    setFilter((current) =>
      current.strategyId === selected
        ? current
        : { strategyId: selected, ticker: undefined },
    );
  }, [artifact, selected, choose]);

  if (!type) return null;
  const ticker = filter.strategyId === selected ? filter.ticker : undefined;

  return (
    <div data-crypto-markets className="cm-panel-content">
      <div className="cm-toolbar cm-panel-selector">
        <span className="cm-panel-strategy-name text-sm font-medium text-[var(--text-primary)]">
          {strategy?.name ?? selected.toUpperCase()}
        </span>
        <StrategyToggle value={selected} onChange={choose} label="详情策略" variant="artifact" />
      </div>
      <div className="cm-panel-source">
        <span className="cm-meta">快照：{date(data?.generated_at ?? null)}</span>
        <button className="cm-link-button" type="button" disabled={loading} onClick={refresh}>
          {loading ? "刷新中…" : "刷新数据"}
        </button>
      </div>
      {(error || stale) && (
        <div className="cm-issue cm-issue-current cm-panel-alert" role="alert">
          <b>{error || "数据快照已过期"}</b>
          <p className="cm-note">
            {data
              ? `下方保留 ${date(data.generated_at)} 的旧快照，不代表当前状态。`
              : "没有可用快照，待数据导出成功后刷新。"}
          </p>
        </div>
      )}
      {["orders", "positions", "settlements", "execution"].includes(type) && (
        <div className="cm-source-banner">
          <strong>Kalshi Demo · 执行账本</strong>
          <span>此页仅展示 Demo 归属记录；纸面主评估见「收益与回撤」，Prod 交易未接入。</span>
        </div>
      )}
      {strategy ? (
        <CryptoArtifact
          key={`${type}:${selected}`}
          type={type}
          strategy={strategy}
          marketTicker={ticker}
          now={now}
        />
      ) : (
        <Empty>
          {loading
            ? "正在读取策略的市场与交易账本…"
            : "当前策略数据不可用，各账本不会相互替代。"}
        </Empty>
      )}
      {type === "health" && data && data.issues.length > 0 && (
        <section className="cm-section" aria-label="快照运行提示">
          <div className="cm-section-heading">
            <h3>快照运行提示（{data.issues.length}）</h3>
          </div>
          <HealthIssues issues={data.issues} />
        </section>
      )}
    </div>
  );
}
