// RealtimeNavViewer 的展示规则，供面板与聊天 NAV 共用。
// 只换算显示，不估算/重冻任何 k、C、K，也不改变 QC 对账规则。
export interface Holding { id: string; name: string; shares: number; value: number }
export interface NavNode {
  node_id: string; display_name: string; kind: string; value: number;
  parent_id?: string | null; positions_as_of?: string | null; corp_action?: boolean;
  holdings?: Holding[] | null; day_return?: number | null; day_pnl?: number | null;
}
export interface NavLatest {
  ts: string; structure_hash: string; stale: boolean; market: string;
  feed_delay_min: number | null; missing: string[]; nodes: NavNode[];
  last_rebuild_ts?: string | null; structure_diff?: string[];
  corp_actions?: Record<string, string>; rebuild_error?: string | null;
  rebuild_error_age_s?: number | null;
}
export interface Cohort { cohort: string; m: number }
export interface MirrorState {
  scalars: Record<string, number>;
  frozen_at?: string | null;
  capital_base?: Record<string, number>;
  cohorts?: Record<string, Record<string, Cohort>>;
  scaled_frozen?: boolean;
  rolloff?: {
    frozen_at: string; measured_on: string; k_equity: number;
    qc_equity?: number; panel_official_total?: number;
  } | null;
}
export type OfficialEquity = Record<string, { date: string; value: number } | undefined>;
export interface PrevClose { date: string | null; values: Record<string, number> }
export type StreamRow = Record<string, string | number | null | undefined>;
export type CheckState = 'pass' | 'fail' | 'pending';
export const OFFICIAL_KEY: Record<string, string> = {
  MRPT: 'mrpt', MTFS: 'mtfs', SSRS: 'ssrs', AISS: 'aiss', AEUS: 'aeus', BDC: 'bdc',
};
export const MULTIPLICATIVE = new Set(['SSRS', 'AISS', 'AEUS', 'BDC']);
export const ADDITIVE = new Set(['MRPT', 'MTFS']);

export const bankersRound = (x: number) => {
  const f = Math.floor(x), d = x - f;
  return d > 0.5 ? f + 1 : d < 0.5 ? f : (f % 2 === 0 ? f : f + 1);
};
export const formatNavMoney = (v: number) =>
  v.toLocaleString(undefined, { maximumFractionDigits: 0 });
export const formatNavPercent = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
// Intl 的负数半位舍入与 Math.round 不同；工具的数值也匹配面板显示。
export const roundedNavMoney = (v: number) =>
  Number(v.toLocaleString('en-US', { maximumFractionDigits: 0, useGrouping: false }));

export function officialAnchor(name: string, value: number,
                               official: OfficialEquity | null,
                               mirror: MirrorState | null) {
  const off = official?.[OFFICIAL_KEY[name]];
  if (!off) return null;
  if (ADDITIVE.has(name)) {
    const C = mirror?.capital_base?.[OFFICIAL_KEY[name]];
    return typeof C === 'number' ? { ...off, live: value - C } : null;
  }
  const k = mirror?.scalars?.[OFFICIAL_KEY[name]];
  return typeof k === 'number' && k > 0 ? { ...off, live: value * k } : null;
}

export function previousByName(latest: NavLatest | null, prevClose: PrevClose | null) {
  const result: Record<string, number> = {};
  if (!prevClose?.values || !latest) return result;
  for (const n of latest.nodes) {
    const value = prevClose.values[n.node_id];
    if (value !== undefined) result[n.display_name] = value;
  }
  return result;
}

export function dayPercent(name: string, value: number, latest: NavLatest | null,
                           prevByName: Record<string, number>, streamRows: StreamRow[]) {
  const node = latest?.nodes.find(n => n.display_name === name);
  if (node && node.day_return !== undefined && node.day_return !== null)
    return node.day_return * 100;
  const base = prevByName[name]
    ?? (streamRows.find(r => r.display_name === name)?.value as number | undefined);
  return base ? (value / base - 1) * 100 : null;
}

const ET_HMS = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});
export function navFeed(latest: NavLatest, nowMs: number) {
  const ageSeconds = Math.max(0, (nowMs - new Date(latest.ts).getTime()) / 1000);
  const ageText = ageSeconds >= 3600
    ? `${Math.floor(ageSeconds / 3600)}h${Math.floor((ageSeconds % 3600) / 60)}m`
    : `${Math.floor(ageSeconds / 60)}m`;
  const delayMin = latest.feed_delay_min ?? 0;
  const price = new Date(new Date(latest.ts).getTime() - delayMin * 60000);
  return { ageSeconds, dead: ageSeconds > 600, lagging: ageSeconds > 180, ageText,
    delayMin, priceTs: price.toISOString(), priceTimeEt: ET_HMS.format(price),
    timeEt: ET_HMS.format(new Date(latest.ts)) };
}

export function navQuality(latest: NavLatest, reconcile: any, allOfficial: boolean,
                           feed: ReturnType<typeof navFeed>) {
  const stale = reconcile?.stale === true;
  const states: Record<string, CheckState> = {
    dual_engine_match: 'pass',
    heartbeat: feed.dead ? 'fail' : feed.lagging ? 'pending' : 'pass',
    price_fresh: latest.stale ? 'fail' : 'pass',
    full_book_quotes: latest.missing?.length ? 'fail' : 'pass',
    reconcile: reconcile?.verdict === 'breach' ? 'fail'
      : stale ? 'pending' : reconcile?.verdict === 'ok' ? 'pass' : 'pending',
    official_anchor: allOfficial ? 'pass' : 'fail',
    structure_sync: !latest.rebuild_error ? 'pass'
      : (latest.rebuild_error_age_s ?? 0) < 600 ? 'pending' : 'fail',
  };
  const allPass = Object.values(states).every(s => s === 'pass');
  const anyFail = Object.values(states).some(s => s === 'fail');
  return { states, allPass, anyFail, status: (allPass ? 'pass' : anyFail ? 'fail' : 'pending') as CheckState };
}

export function holdingsPresentation(h: Holding, strategy: NavNode,
                                     official: OfficialEquity | null,
                                     mirror: MirrorState | null, cohort?: Cohort) {
  const name = strategy.display_name;
  const scalable = MULTIPLICATIVE.has(name);
  const oa = officialAnchor(name, strategy.value, official, mirror);
  const scale = ADDITIVE.has(name) ? 1 : (oa && strategy.value ? oa.live / strategy.value : 1);
  const k = scalable ? mirror?.scalars?.[OFFICIAL_KEY[name]] : undefined;
  const missing = scalable && !(typeof k === 'number' && k > 0);
  const shares = typeof k === 'number' && k > 0 ? bankersRound(h.shares * k) : h.shares;
  const qc = cohort ? (cohort.m === 0 ? 0 : bankersRound(h.shares * cohort.m))
    : (scalable && !missing ? shares : null);
  return { ticker: h.name, id: h.id, shares, ledger_shares: h.shares,
    qc_shares: qc, value: h.value * scale, display_value: formatNavMoney(h.value * scale),
    missing_scalar: missing, cohort: cohort ?? null };
}

// 展开面板的资金构成。K/队列差额仅作信息项，不加入净值。
export function capitalPresentation(s: NavNode, kids: NavNode[],
                                    cohorts: Record<string, Cohort> | undefined,
                                    mirror: MirrorState | null, mul?: { k: number }) {
  const C = mirror?.capital_base?.[OFFICIAL_KEY[s.display_name]];
  const scale = mul ? mul.k : 1;
  let Lmv = 0, Smv = 0;
  const addLeg = (v: number) => { if (v >= 0) Lmv += v; else Smv += -v; };
  if (kids.length) {
    for (const k of kids) {
      const hs = k.holdings || [];
      if (hs.length === 0) { addLeg(k.value * scale); continue; }
      for (const h of hs) addLeg(h.value * scale);
    }
  } else {
    for (const h of s.holdings || []) addLeg(h.value * scale);
  }
  const E = s.value * scale;
  const shortDue = 0.02 * Smv;
  const restrictedCash = 1.02 * Smv;
  const marginLoan = Math.max(0, Lmv + shortDue - E);
  const freeCash = Math.max(0, E - Lmv - shortDue);
  const gross = Lmv + Smv;
  const levLedger = E > 0 ? gross / E : null;
  const levOfficial = mul ? levLedger : (C !== undefined && E - C > 0 ? gross / (E - C) : null);
  const byCohort: Record<string, number> = { L: 0, S: 0, F: 0 };
  let unknown = false;
  for (const k of kids) {
    const cohort = cohorts?.[k.display_name];
    if (!cohort) { unknown = true; continue; }
    for (const h of k.holdings || []) {
      if (!h.shares) continue;
      const px = h.value / h.shares;
      const qcSh = cohort.m === 0 ? 0 : bankersRound(h.shares * cohort.m);
      byCohort[cohort.cohort] += (h.shares - qcSh) * px;
    }
  }
  return { C, scale, Lmv, Smv, E, shortDue, restrictedCash, marginLoan, freeCash,
    gross, levLedger, levOfficial, byCohort, unknown, gap: byCohort.L + byCohort.S + byCohort.F };
}

export interface NavPanelInput {
  latest: NavLatest; official: OfficialEquity | null; mirror: MirrorState | null;
  prevClose: PrevClose | null; streamRows?: StreamRow[]; reconcile: any; nowMs: number;
}

export function buildRealtimeNavPanel(input: NavPanelInput) {
  const { latest, official, mirror, prevClose, reconcile, nowMs, streamRows = [] } = input;
  const strategies = latest.nodes.filter(n => n.kind === 'strategy');
  const pf = latest.nodes.find(n => n.kind === 'portfolio');
  const previous = previousByName(latest, prevClose);
  const dayPct = (name: string, value: number) => dayPercent(name, value, latest, previous, streamRows);
  const parts = strategies.map(s => officialAnchor(s.display_name, s.value, official, mirror));
  const allOfficial = parts.length > 0 && parts.every(Boolean);
  const main = allOfficial ? parts.reduce((a, p) => a + p!.live, 0) : pf?.value ?? 0;
  const offSum = allOfficial ? parts.reduce((a, p) => a + p!.value, 0) : 0;
  const pct = allOfficial && offSum ? (main / offSum - 1) * 100
    : pf ? dayPct('PORTFOLIO', pf.value) : null;
  const feed = navFeed(latest, nowMs);
  return {
    portfolio: {
      value: main, official_anchored_value: allOfficial ? main : null,
      basis: allOfficial ? 'official' as const : 'ledger' as const,
      day_return_pct: pct, display_value: formatNavMoney(main),
      display_return: pct === null ? null : formatNavPercent(pct),
      official_eod_total: offSum,
      comparison_date: Object.keys(previous).length > 0 && prevClose?.date ? prevClose.date : null,
    },
    strategies: strategies.map((s, i) => {
      const oa = parts[i];
      const value = oa ? oa.live : s.value;
      const pct = oa && oa.value ? (oa.live / oa.value - 1) * 100 : dayPct(s.display_name, s.value);
      const cohorts = ADDITIVE.has(s.display_name) ? mirror?.cohorts?.[OFFICIAL_KEY[s.display_name]] : undefined;
      const kids = latest.nodes.filter(n => n.parent_id === s.node_id && n.kind !== 'strategy');
      const scale = ADDITIVE.has(s.display_name) ? 1 : (oa && s.value ? oa.live / s.value : 1);
      const k = mirror?.scalars?.[OFFICIAL_KEY[s.display_name]];
      const showMulCapital = MULTIPLICATIVE.has(s.display_name) && typeof k === 'number' && k > 0
        && (kids.length > 0 || !!s.holdings?.length);
      const showAddCapital = ADDITIVE.has(s.display_name) && (kids.length > 0 || !s.holdings?.length);
      const children = kids.map(k => {
        const holdings = (k.holdings || []).map(h => holdingsPresentation(h, s, official, mirror, cohorts?.[k.display_name]));
        const long = holdings.reduce((a, h) => a + Math.max(h.value, 0), 0);
        const short = holdings.reduce((a, h) => a + Math.max(-h.value, 0), 0);
        return { node_id: k.node_id, name: k.display_name, kind: k.kind,
          value: k.value * scale, long_value: long, short_value: short,
          day_return_pct: dayPct(k.display_name, k.value), holdings,
          cohort: cohorts?.[k.display_name] ?? null };
      });
      return {
        node_id: s.node_id, strategy: s.display_name, value,
        official_anchored_value: oa ? oa.live : null, official_eod: official?.[OFFICIAL_KEY[s.display_name]] ?? null,
        basis: oa ? 'official' as const : 'ledger' as const,
        day_return_pct: pct, display_value: formatNavMoney(value),
        display_return: pct === null ? null : formatNavPercent(pct),
        positions_as_of: s.positions_as_of ?? null, corp_action: !!s.corp_action,
        // 展开的子节点才是 AISS/AEUS/pairs 的持股行；不把父层和子层重复加总。
        holdings: kids.length ? children.flatMap(k => k.holdings)
          : (s.holdings || []).map(h => holdingsPresentation(h, s, official, mirror)),
        children,
        capital: showMulCapital ? capitalPresentation(s, kids, undefined, mirror, { k: k! })
          : showAddCapital ? capitalPresentation(s, kids, cohorts, mirror) : null,
      };
    }),
    feed, quality: navQuality(latest, reconcile, allOfficial, feed),
    rolloff: mirror?.rolloff ?? null,
  };
}
