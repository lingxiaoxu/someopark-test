import { ChevronRight, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useSetArtifact } from "../contexts/ArtifactContext";
import { ARTIFACTS } from "./Artifacts";
import { useCryptoPrediction } from "./CryptoContext";
import StrategyToggle from "./StrategyToggle";
import { age, money, number, publicText } from "./primitives";
import type { Market, Strategy } from "./types";
import { resultNetLabel } from "./resultBooks";
import { selectHomeMarkets } from "./homeMarkets";
import "./embedded.css";

function closeTime(value: string | null) {
  if (!value) return "结束时间未取得";
  return `${new Date(value).toISOString().slice(5, 16).replace("T", " ")} UTC`;
}

function quote(value: number | null) {
  return value === null ? "无报价" : `$${value.toFixed(4)}`;
}

function participation(strategy: Strategy, market: Market) {
  const orders = strategy.orders.filter((order) => order.ticker === market.ticker);
  if (!orders.length) return `${strategy.acronym} · Demo 暂无归属订单`;
  const verified = orders.filter((order) => order.verified && order.filled !== null);
  if (!verified.length) return `${strategy.acronym} · Demo ${orders.length} 单，成交待核验`;
  const filled = verified.reduce((total, order) => total + order.filled!, 0);
  const pending = orders.length - verified.length;
  return `${strategy.acronym} · Demo 已核验成交 ${number(filled)} 张${pending ? ` · ${pending} 单待核验` : ` · ${orders.length} 单`}`;
}

/** Uses the existing welcome-card chrome; App and ChatArea own all navigation. */
export function CryptoUpcoming() {
  const { data, error, loading, refresh, selected, choose, setResultBasis, now, stale, issues, strategy } = useCryptoPrediction();
  const setArtifact = useSetArtifact();
  const [view, setView] = useState<"active" | "recent">("active");
  const marketRows = selectHomeMarkets(strategy?.markets ?? [], now);
  const recent = [...(strategy?.settlements ?? [])].sort((a, b) => String(b.at).localeCompare(String(a.at)));
  const settlements = recent.slice(0, 6);
  const count = strategy ? (view === "active" ? marketRows.filter(row => row.state === "open").length : recent.length) : null;
  const awaiting = strategy?.positions.filter((position) => position.verified && position.close_at && Date.parse(position.close_at) <= now).length ?? 0;
  const unverified = strategy?.positions.filter((position) => !position.verified).length ?? 0;
  const openHealth = () => setArtifact({ type: "crypto_health", title: "运行与数据健康" });

  return (
    <section className="crypto-home crypto-upcoming p-4 relative" aria-label="二元市场总览">
      {["tl", "tr", "bl", "br"].map((corner) => <span key={corner} className={`crypto-corner crypto-corner-${corner}`} />)}
      <div className="flex items-center justify-between mb-3 crypto-overview-heading">
        <div className="crypto-overview-title">
          二元市场 <span className="crypto-positive">({count === null ? "—" : count})</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="crypto-overview-environment">高频加密货币预测市场</span>
          <StrategyToggle value={selected} onChange={choose} label="总览策略" />
        </div>
      </div>
      {strategy && (
        <div className="crypto-result-summary" aria-label={`${strategy.acronym} 独立结果账本`}>
          <button type="button" className="crypto-paper-result" onClick={() => {
            setResultBasis("paper");
            setArtifact({ type: "crypto_performance", title: "收益与回撤", strategyId: selected });
          }} aria-label={`${strategy.acronym} 纸面策略评估详情`} title={`${resultNetLabel("paper", strategy.paper)} · 来源 ${age(strategy.paper.source_as_of, now)} · 点击查看收益与回撤`}>
            <span>{strategy.paper.status === "partial" ? "策略净收益（部分待核验）" : "策略净收益"}</span>
            <strong className={strategy.paper.net_pnl_usd !== null && strategy.paper.net_pnl_usd < 0 ? "crypto-negative" : "crypto-positive"}>
              {strategy.paper.net_pnl_usd !== null && strategy.paper.net_pnl_usd > 0 ? "+" : ""}{money(strategy.paper.net_pnl_usd)}
            </strong>
          </button>
          <span className="crypto-result-divider" aria-hidden="true">/</span>
          <button type="button" className="crypto-demo-result" onClick={() => {
            setResultBasis("demo");
            setArtifact({ type: "crypto_performance", title: "收益与回撤", strategyId: selected });
          }} aria-label={`${strategy.acronym} Kalshi Demo 执行验证详情`} title={`${resultNetLabel("demo", strategy.demo)} · 来源 ${age(strategy.demo.source_as_of, now)} · 点击查看独立执行账本`}>
            <span>Demo 已镜像部分</span>
            <strong className={strategy.demo.net_pnl_usd !== null && strategy.demo.net_pnl_usd < 0 ? "crypto-negative" : "crypto-positive"}>{strategy.demo.net_pnl_usd !== null && strategy.demo.net_pnl_usd > 0 ? "+" : ""}{money(strategy.demo.net_pnl_usd)}</strong>
          </button>
        </div>
      )}
      <div className="crypto-overview-controls">
        <span>{strategy ? `${strategy.acronym} · ${strategy.name}` : `${selected.toUpperCase()} · 数据待取得`}</span>
        <div className="crypto-view-tabs" role="group" aria-label="市场时段">
          <button type="button" aria-pressed={view === "active"} onClick={() => setView("active")}>当前合约</button>
          <button type="button" aria-pressed={view === "recent"} onClick={() => setView("recent")}>Demo 近期结算</button>
        </div>
      </div>

      {error && (
        <div className="crypto-home-status crypto-negative" role="alert">
          数据读取失败：{publicText(error)}
          {data && "；以下保留上次成功快照。"}
        </div>
      )}
      {!strategy ? (
        <div className="text-xs py-2" style={{ color: "var(--text-muted)" }}>
          {loading ? "正在读取加密货币策略数据…" : "当前没有可核验的策略数据。"}
        </div>
      ) : (
        <div className="crypto-market-list">
          {view === "active" && marketRows.map(({ asset, state, market }) => {
            if (!market) {
              const notice = state === "awaiting_next" ? "本期已结束，等待下一期合约录制"
                : state === "stale" ? "行情录制过期，等待更新" : "当前合约状态待确认";
              return (
                <div key={asset} className="pair-card crypto-market-row" style={{ cursor: "default" }}
                  role="group" aria-label={`${asset} ${notice}`}>
                  <span className="crypto-market-row-heading">
                    <span><strong>{asset}</strong><span className="crypto-home-muted">UP / DOWN · 15 分钟</span></span>
                    <span className="crypto-caution">等待更新</span>
                  </span>
                  <span className="crypto-market-row-detail crypto-caution">{notice}</span>
                  <span className="crypto-market-row-detail crypto-home-muted">
                    <span>{strategy.acronym} · 数据更新后自动显示</span>
                    <span>未计入开放合约数</span>
                  </span>
                </div>
              );
            }
            const oldQuote = !market.quote_at || now - Date.parse(market.quote_at) > 180_000;
            return (
              <button
                type="button"
                key={market.ticker}
                className="pair-card crypto-market-row"
                onClick={() => setArtifact({ type: "crypto_markets", title: "二元市场", strategyId: selected, ticker: market.ticker })}
                aria-label={`${market.asset} ${closeTime(market.close_at)} 合约详情`}
              >
                <span className="crypto-market-row-heading">
                  <span><ChevronRight className="w-3 h-3" /><strong>{market.asset}</strong><span className="crypto-home-muted">UP / DOWN · 15 分钟</span></span>
                  <time>{closeTime(market.close_at)}</time>
                </span>
                <span className="crypto-market-row-detail">
                  <span>阈值 {market.threshold === null ? "未取得" : `$${number(market.threshold, 8)}`}</span>
                  <span>YES {quote(market.yes_ask)} · NO {quote(market.no_ask)}</span>
                </span>
                <span className="crypto-market-row-detail">
                  <span>{participation(strategy, market)}</span>
                  <span className={oldQuote ? "crypto-caution" : "crypto-home-muted"}>
                    {oldQuote ? "行情过期" : market.quote_source === "orderbook" ? "Prod 盘口" : market.quote_source === "market_summary" ? "Prod 摘要价" : "行情未取得"}
                  </span>
                </span>
              </button>
            );
          })}
          {view === "recent" && settlements.map((settlement) => (
            <button
              type="button"
              key={settlement.ticker}
              className="pair-card crypto-market-row"
              onClick={() => setArtifact({ type: "crypto_settlements", title: "结算账本", strategyId: selected, ticker: settlement.ticker })}
              aria-label={`${settlement.asset} ${closeTime(settlement.at)} Demo 结算详情`}
            >
              <span className="crypto-market-row-heading">
                <span><ChevronRight className="w-3 h-3" /><strong>{settlement.asset}</strong><span className="crypto-home-muted">{settlement.result.toUpperCase()} 结算</span></span>
                <time>{closeTime(settlement.at)}</time>
              </span>
              <span className="crypto-market-row-detail">
                <span>{strategy.acronym} · Demo 归属成交 {number(settlement.quantity)} 张</span>
                <strong className={settlement.net_usd < 0 ? "crypto-negative" : "crypto-positive"}>{settlement.net_usd > 0 ? "+" : ""}{money(settlement.net_usd)}</strong>
              </span>
              <span className="crypto-market-row-detail crypto-home-muted">
                <span>成本 {money(settlement.cost_usd)} · 费用 {money(settlement.fees_usd)}</span>
                <span>已结算净收益</span>
              </span>
            </button>
          ))}
          {!(view === "active" ? marketRows.length : settlements.length) && (
            <div className="text-xs py-2" style={{ color: "var(--text-muted)" }}>
              {view === "active" ? "当前市场录制未取得，等待数据更新；不能据此判断交易所没有合约。" : "当前策略没有可核验的 Demo 结算记录。"}
            </div>
          )}
        </div>
      )}

      {(awaiting > 0 || unverified > 0) && (
        <div className="crypto-home-status crypto-caution">
          {awaiting > 0 && `${awaiting} 个归属市场到期等待官方结算。`}
          {unverified > 0 && `${unverified} 个市场仓位待核验。`}
          未计入已实现收益。
        </div>
      )}
      <div className="crypto-overview-footer">
        <span>
          {view === "active" ? "Prod 行情 · Demo 执行" : `Demo 官方结算 · 最近 ${settlements.length} 个市场`}
          {data && ` · 快照 ${age(data.generated_at, now)}`}
        </span>
        <span className="flex items-center gap-2">
          {(stale || issues.length > 0) && <button type="button" className="crypto-caution" onClick={openHealth}>{stale ? "快照过期" : `${issues.length} 项数据提示`}</button>}
          <button type="button" onClick={() => { void refresh(); }} disabled={loading} aria-label="刷新加密货币数据" title="刷新加密货币数据"><RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /></button>
        </span>
      </div>
    </section>
  );
}

/** Class names intentionally match PredictionArtifactGrid to reuse its UI. */
export function CryptoArtifactGrid({ categorized = false }: { categorized?: boolean }) {
  const setArtifact = useSetArtifact();
  const renderGrid = (items: typeof ARTIFACTS) => (
    <div className="grid grid-cols-2 gap-2">
      {items.map(({ type, title, icon: Icon }) => (
        <button
          type="button"
          key={type}
          onClick={() => setArtifact({ type: `crypto_${type}`, title })}
          className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]"
        >
          <Icon className="w-4 h-4 text-[var(--accent-primary)]" /> {title}
        </button>
      ))}
    </div>
  );

  return (
    <div className="crypto-home crypto-artifact-grid">
      {categorized ? (
        <div className="flex flex-col gap-3">
          {[...new Set(ARTIFACTS.map((item) => item.group))].map((group) => (
            <div key={group}>
              <div className="flex items-center gap-1.5 mb-1.5 text-[10px] font-bold uppercase tracking-wider text-[var(--text-muted)]">{group}</div>
              {renderGrid(ARTIFACTS.filter((item) => item.group === group))}
            </div>
          ))}
        </div>
      ) : renderGrid(ARTIFACTS)}
    </div>
  );
}

export default function CryptoHome({ categorized = false }: { categorized?: boolean }) {
  return <><CryptoUpcoming /><CryptoArtifactGrid categorized={categorized} /></>;
}
