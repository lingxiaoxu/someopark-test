// server/routes/volumePrediction.ts — VolumePrediction 每日产出(只读)
// 只读 VolumePrediction/outputs/(与 controllerNav 同纪律:server 读别人输出,
// 绝不写 VolumePrediction 下任何文件)。数据由 launchd vp.shadowdaily 17:33 ET 生成。
import { Router } from 'express';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(__dirname, '..', '..', '..');
const OUT = path.join(REPO, 'VolumePrediction', 'outputs');

const router = Router();

// advice 文件名前缀:pairs 族带 pairs_ 前缀,qlib 族不带
const ADVICE_PREFIX: Record<string, string> = {
  mrpt: 'pairs_mrpt', mtfs: 'pairs_mtfs', aiss: 'aiss', ssrs: 'ssrs',
};

function latestAdvice(strategy: string): any | null {
  const prefix = ADVICE_PREFIX[strategy];
  if (!prefix) return null;
  const dir = path.join(OUT, 'adapters');
  if (!fs.existsSync(dir)) return null;
  const re = new RegExp(`^${prefix}_advice_(\\d{4}-\\d{2}-\\d{2})\\.json$`);
  const files = fs.readdirSync(dir).filter(f => re.test(f)).sort();
  if (!files.length) return null;
  return JSON.parse(fs.readFileSync(path.join(dir, files[files.length - 1]), 'utf-8'));
}

// blend AB 追踪(滞后口径:pred_date 的预测 vs actual_date 的实际)——
// 只取面板要用的列,CSV 全量列留在磁盘上不进接口
const AB_COLS = ['pred_date', 'actual_date', 'n_held',
  'blend3_held_mape', 'prod_held_mape', 'blend3_mape', 'prod_mape'];

function abRecent(n: number): Record<string, string | number>[] {
  const p = path.join(OUT, 'shadow_blend', 'blend_ab_tracking.csv');
  if (!fs.existsSync(p)) return [];
  const lines = fs.readFileSync(p, 'utf-8').trim().split('\n');
  if (lines.length < 2) return [];
  const header = lines[0].split(',');
  const idx = AB_COLS.map(c => header.indexOf(c));
  return lines.slice(-n).map(l => {
    const v = l.split(',');
    const o: Record<string, string | number> = {};
    AB_COLS.forEach((c, i) => {
      const raw = idx[i] >= 0 ? v[idx[i]] : '';
      o[c] = c.endsWith('date') ? raw : Number(raw);
    });
    return o;
  });
}

// 持仓明细 join(只读):advice 文件只有流动性字段,PairBadge 统一容器要的
// 方向/股数/权重/价格在各策略 inventory 里 —— server 侧拼好,前端照
// SignalTable/InventoryViewer 的既有适配传参,不在前端重实现策略差异。
const INV_PATH: Record<string, string> = {
  mrpt: 'inventory_mrpt.json',
  mtfs: 'inventory_mtfs.json',
  ssrs: path.join('qlib-main', 'sector_rotation', 'inventory_sector_rotation.json'),
  aiss: path.join('qlib-main', 'semiconductor_strategy', 'inventory_aiss.json'),
};

const mapPairEntry = (v: any) => ({
  direction: v.direction, s1_shares: v.s1_shares, s2_shares: v.s2_shares,
  s1_price: v.open_s1_price, s2_price: v.open_s2_price,
  open_date: v.open_date, days_held: v.days_held,
  hedge_ratio: v.open_hedge_ratio, param_set: v.param_set,
});

// advice 是 D-1 的:当日盘中平掉的对(如 8/25 早的 PAYC/LII)已不在当前
// inventory,join 落空 → 前端裸 badge。回退到 inventory_history/ 快照
// (平仓时的**改写前副本**必含该对)补齐明细,并标 closed 让前端能画"已平仓"。
function pairsFallback(strategy: string, missing: string[]): Record<string, any> {
  const dir = path.join(REPO, 'inventory_history');
  if (!missing.length || !fs.existsSync(dir)) return {};
  const re = new RegExp(`^inventory_${strategy}_\\d{8}_\\d{6}\\.json$`);
  const files = fs.readdirSync(dir).filter(f => re.test(f)).sort().reverse().slice(0, 12);
  const out: Record<string, any> = {};
  for (const f of files) {
    if (missing.every(m => m in out)) break;
    try {
      const pairs = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf-8')).pairs ?? {};
      for (const m of missing) {
        if (out[m] || !pairs[m]?.direction) continue;
        out[m] = { ...mapPairEntry(pairs[m]), closed: true };
      }
    } catch { /* 半写/坏快照 → 跳过,继续往older找 */ }
  }
  return out;
}

function inventoryDetails(strategy: string): Record<string, any> {
  const p = path.join(REPO, INV_PATH[strategy]);
  if (!fs.existsSync(p)) return {};
  try {
    const inv = JSON.parse(fs.readFileSync(p, 'utf-8'));
    const out: Record<string, any> = {};
    if (strategy === 'mrpt' || strategy === 'mtfs') {
      for (const [pair, v] of Object.entries<any>(inv.pairs ?? {})) {
        if (!v?.direction) continue;
        out[pair] = mapPairEntry(v);
      }
    } else if (strategy === 'ssrs') {
      for (const [etf, v] of Object.entries<any>(inv.holdings ?? {})) {
        out[etf] = { weight: v.weight, shares: v.shares, last_price: v.last_price,
          cost_basis: v.cost_basis, open_date: v.entry_date, days_held: v.days_held };
      }
    } else {
      for (const [tk, v] of Object.entries<any>(inv.stock_holdings ?? {})) {
        out[tk] = { weight: v.portfolio_weight, shares: v.shares, last_price: v.last_price,
          cost_basis: v.cost_basis, open_date: v.entry_date, days_held: v.days_held };
      }
    }
    return out;
  } catch {
    return {};
  }
}

// 候选对来自 pair_universe(无方向)—— 方向/Z/价在当天 signals 文件里
// (SignalTable 同源)。join 上让候选 badge 与信号页同款配色/popup 明细。
function latestSignals(strategy: string): Record<string, any> {
  const dir = path.join(REPO, 'trading_signals');
  if (!fs.existsSync(dir)) return {};
  const re = new RegExp(`^${strategy}_signals_\\d{8}_\\d{6}\\.json$`);
  const files = fs.readdirSync(dir).filter(f => re.test(f)).sort();
  if (!files.length) return {};
  try {
    const sigs = JSON.parse(
      fs.readFileSync(path.join(dir, files[files.length - 1]), 'utf-8')).signals ?? [];
    const out: Record<string, any> = {};
    for (const s of sigs) {
      if (!s?.pair) continue;
      out[s.pair] = { action: s.action, direction: s.direction ?? null,
        z_score: s.z_score, momentum_spread: s.momentum_spread,
        s1_price: s.s1_price, s2_price: s.s2_price };
    }
    return out;
  } catch {
    return {};
  }
}

// 候选池 = 当天 signals ∪ daily_report excluded_pairs(OOS 不达标被排除的对)。
// 排除对没有信号行,join 进来才知道"为什么中性"——前端标已排除+原因。
function excludedPairs(strategy: string): Record<string, any> {
  const dir = path.join(REPO, 'trading_signals');
  if (!fs.existsSync(dir)) return {};
  const re = /^daily_report_\d{8}_\d{6}\.json$/;
  const files = fs.readdirSync(dir).filter(f => re.test(f)).sort();
  if (!files.length) return {};
  try {
    const rep = JSON.parse(
      fs.readFileSync(path.join(dir, files[files.length - 1]), 'utf-8'));
    const out: Record<string, any> = {};
    for (const e of rep?.[strategy]?.excluded_pairs ?? []) {
      if (!e?.pair) continue;
      out[e.pair] = { excluded: true, reason: e.exclusion_reason,
        oos_sharpe: e.oos_sharpe };
    }
    return out;
  } catch {
    return {};
  }
}

// 实际 serving mix:最近一行 AB 的 blend3_mix(如
// "baselines.ma5:8627;rnn_v6f32n_20260731:3852")。registry 的 production 槽
// 有意留在 lgbm(8/17 promote 决定),真实出预测的是 blend3 路由 —— 面板显示这个。
function servingMix(): { model: string; n: number }[] {
  const p = path.join(OUT, 'shadow_blend', 'blend_ab_tracking.csv');
  if (!fs.existsSync(p)) return [];
  const lines = fs.readFileSync(p, 'utf-8').trim().split('\n');
  if (lines.length < 2) return [];
  const col = lines[0].split(',').indexOf('blend3_mix');
  if (col < 0) return [];
  const raw = lines[lines.length - 1].split(',')[col] || '';
  return raw.split(';').filter(Boolean).map(part => {
    const [model, n] = part.split(':');
    return { model, n: Number(n) || 0 };
  });
}

// 单端点:health + 当前策略最新 advice + AB 近 5 行,切策略一次取齐
router.get('/daily/:strategy', (req, res) => {
  const strategy = String(req.params.strategy);
  if (!ADVICE_PREFIX[strategy]) return res.status(400).json({ error: 'unknown strategy' });
  try {
    const healthPath = path.join(OUT, 'service_health.json');
    const health = fs.existsSync(healthPath)
      ? JSON.parse(fs.readFileSync(healthPath, 'utf-8')) : null;
    const advice = latestAdvice(strategy);
    if (!health && !advice) {
      return res.status(404).json({ error: 'no VolumePrediction outputs yet' });
    }
    const inv = inventoryDetails(strategy);
    const pairsMode = strategy === 'mrpt' || strategy === 'mtfs';
    if (pairsMode && advice?.positions) {
      const missing = advice.positions
        .filter((p: any) => ((p.s1_dtl || 0) > 0 || (p.s2_dtl || 0) > 0) && !inv[p.pair])
        .map((p: any) => p.pair);
      Object.assign(inv, pairsFallback(strategy, missing));
    }
    res.json({ health, advice, ab: abRecent(5), serving_mix: servingMix(), inv,
      sig: pairsMode ? { ...excludedPairs(strategy), ...latestSignals(strategy) } : {} });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;
