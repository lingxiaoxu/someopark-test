// server/routes/controllerNav.ts — 实时净值看板数据(M7)
// 只读 controller/output/(controller 纪律 6:它只写自己目录;server 读别人输出,
// 两侧边界干净)。绝不写任何 controller 文件。
import { Router } from 'express';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(__dirname, '..', '..', '..');
const OUT = path.join(REPO, 'controller', 'output');
const DATA = path.join(__dirname, '..', '..', 'public', 'data');

const router = Router();

// "今天"必须按 ET 算(2026-08-14 bug):toISOString 是 UTC,20:00 ET 后 UTC 已翻日,
// /stream 默认日指向不存在的明天文件、/prev-close 把**今天**的收盘当"昨收"返回。
const etToday = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' })
  .format(new Date()).replace(/-/g, '');

// 最新全层级值(前端 45s 轮询)
router.get('/latest', (_req, res) => {
  const p = path.join(OUT, 'nav_latest.json');
  if (!fs.existsSync(p)) return res.status(404).json({ error: 'controller not running yet' });
  res.json(JSON.parse(fs.readFileSync(p, 'utf-8')));
});

// 当日分钟流(频率子采样在前端做;date=YYYYMMDD 默认今天;
// date=latest → 最近一个有数据(≥2 行)的交易时段,闭市回看用)
router.get('/stream', (req, res) => {
  const today = etToday();
  let date = String(req.query.date || today);
  if (date === 'latest') {
    const days = fs.existsSync(OUT)
      ? fs.readdirSync(OUT)
          .map(f => /^nav_stream_(\d{8})\.csv$/.exec(f)?.[1])
          .filter((d): d is string => !!d).sort()
      : [];
    date = '';
    for (let i = days.length - 1; i >= 0; i--) {
      if (readStreamMerged(days[i]).length >= 2) { date = days[i]; break; }
    }
    if (!date) return res.json({ date: null, rows: [] });
  }
  res.json({ date, rows: readStreamMerged(date) });
});

// 读取一个交易日的完整流:schema 轮转分段(.v1, .v2 …)按序 + 主文件,
// 各段用自己的表头解析(老段缺新列 → 字段缺省),时间序自然衔接
function readStreamMerged(date: string): Record<string, string | number>[] {
  const main = path.join(OUT, `nav_stream_${date}.csv`);
  const parts = fs.existsSync(OUT)
    ? fs.readdirSync(OUT)
        .map(f => new RegExp(`^nav_stream_${date}\\.csv\\.v(\\d+)$`).exec(f))
        .filter((m): m is RegExpExecArray => !!m)
        .sort((a, b) => Number(a[1]) - Number(b[1]))
        .map(m => path.join(OUT, m[0]))
    : [];
  if (fs.existsSync(main)) parts.push(main);
  const rows: Record<string, string | number>[] = [];
  for (const p of parts) {
    const lines = fs.readFileSync(p, 'utf-8').trim().split('\n');
    if (lines.length < 2) continue;
    const header = lines[0].split(',');
    for (const l of lines.slice(1)) {
      const v = l.split(',');
      const o: Record<string, string | number> = {};
      header.forEach((h, i) => { o[h] = h === 'value' ? Number(v[i]) : v[i]; });
      rows.push(o);
    }
  }
  return rows;
}

// 前一交易日 controller 收盘值 = **今日 day_pnl 的日初账面**(day_state.base_value)。
//
// 2026-08-20 修正:原实现取 ≤16:00 ET 的末笔,但订阅是 15 分钟延迟行情,那一笔实际是
// ~15:45 的价;scheduler 在 ≥16:20 ET 会用官方 daily_close 覆写一遍(见 scheduler.py
// §2c),盘后/隔夜平移续写落在**官方收盘**上。两者不是同一个价源:8/19 MTFS
// ≤16:00 末笔 876,062.86,官方收盘重写后 881,441.04,差 $5,594 —— 拿前者当昨收,
// 隔夜图上就凭空多出 +0.61% 账本(加性族 −C 放大后 **+1.85%**)的假日内收益,
// 而 day_return 同刻是 0。凌晨看图"MTFS 在动"就是这么来的(其实是条非零平线)。
//
// day_state.base_value 是 scheduler 自己给 day_return 当分母的那个数,取它 ⇒ 图的 %
// 与后端发布的 day_return 同分母,构造上不可能再错位。两族都适用:乘性族分子用
// day_return,加性族分子用原始净值差(记账台阶只有加性族有),分母同源。
// 回退顺序:day_state(date=今天)→ 前一日 stream 的**末笔**(已含官方收盘重写)。
router.get('/prev-close', (_req, res) => {
  if (!fs.existsSync(OUT)) return res.status(404).json({ error: 'no output dir' });
  const today = etToday();
  const days = fs.readdirSync(OUT)
    .map(f => /^nav_stream_(\d{8})\.csv$/.exec(f)?.[1])
    .filter((d): d is string => !!d && d < today)
    .sort();
  if (!days.length) return res.json({ date: null, values: {} });
  const date = days[days.length - 1];
  // 首选:scheduler 自己的日初账面(= day_return 的分母,官方收盘口径)
  try {
    const st = JSON.parse(fs.readFileSync(path.join(OUT, 'day_state.json'), 'utf-8'));
    const bv = st?.base_value;
    // scheduler 写的是 et_today() = "YYYY-MM-DD",这里的 today 是 stream 文件名的
    // "YYYYMMDD" —— 去横杠再比,否则本分支永远不命中(静默退化成回退路径)
    const stDay = typeof st?.date === 'string' ? st.date.replace(/-/g, '') : null;
    if (stDay === today && bv && Object.keys(bv).length) {
      const vals: Record<string, number> = {};
      for (const [k, v] of Object.entries(bv)) {
        if (typeof v === 'number' && Number.isFinite(v)) vals[k] = v;
      }
      if (Object.keys(vals).length) return res.json({ date, values: vals, src: 'day_state' });
    }
  } catch { /* 缺文件/半写 → 落到 stream 回退,不静默给错口径 */ }
  // 回退:前一日 stream 的**末笔**。闭市后 scheduler 平移续写,末笔即官方收盘重写后的值;
  // 若当日 EOD 重写没跑成,末笔是盘后末次报价 —— 仍比 ≤16:00 的 15:45 延迟价更近收盘。
  const values: Record<string, number> = {};
  for (const r of readStreamMerged(date)) {
    values[String(r.node_id)] = Number(r.value);
  }
  res.json({ date, values, src: 'stream_last' });
});

// 官方 EOD 锚(V_base;锚定映射与 controller/reconcile_eod.py 一致,原始源)
router.get('/official', (_req, res) => {
  const readLast = (file: string, cols: string[]) => {
    const rows = JSON.parse(fs.readFileSync(path.join(DATA, file), 'utf-8'));
    const out: Record<string, { date: string; value: number }> = {};
    for (const col of cols) {
      for (let i = rows.length - 1; i >= 0; i--) {
        if (rows[i][col] != null) { out[col] = { date: rows[i].date, value: rows[i][col] }; break; }
      }
    }
    return out;
  };
  try {
    const sp = readLast('strategy_performance.json', ['mrpt_equity', 'mtfs_equity']);
    const mp = readLast('master_portfolio_performance.json', ['sr_equity', 'aiss_equity']);
    const bd = readLast('private_credit_bdc_performance.json', ['bdc_equity']);
    res.json({
      mrpt: sp.mrpt_equity, mtfs: sp.mtfs_equity,
      ssrs: mp.sr_equity, aiss: mp.aiss_equity, bdc: bd.bdc_equity,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// go-live 冻结的「官方/账本」资本比例 —— QC 缩放镜像与本看板共用的同一组常数。
// 看板用它把账本股数换算成官方口径股数(见 RealtimeNavViewer 的 MULTIPLICATIVE)。
// 严格只读:看板/server 绝不写 trading_quantconnect 下任何文件(防火墙)。
router.get('/scalars', (_req, res) => {
  const p = path.join(REPO, 'trading_quantconnect', 'state', 'exporter_state.json');
  if (!fs.existsSync(p)) return res.status(404).json({ error: 'no exporter state' });
  try {
    const s = JSON.parse(fs.readFileSync(p, 'utf-8'));
    if (!s.scalars) return res.status(500).json({ error: 'exporter_state has no scalars' });
    // 加性族(MRPT/MTFS)的资本基准差 C:official = ledger − C ⇒ C = ledger − official。
    // 与 scalars 同源于 go-live 那一次冻结,之后不再重算(实测 105 天极差 ≤$0.01)。
    const capitalBase: Record<string, number> = {};
    const bo = s.scalar_basis?.official ?? {}, bl = s.scalar_basis?.ledger ?? {};
    for (const st of ['mrpt', 'mtfs']) {
      if (typeof bo[st] === 'number' && typeof bl[st] === 'number') {
        capitalBase[st] = bl[st] - bo[st];
      }
    }
    // pairs 三队列(镜像倍数在**开仓那一刻**定死): L=legacy m=0 · S=scaled m=k · F m=1。
    // 归属判定必须是 (pair 名, open_date) 二元组 —— 只按名字匹配会把"同名平掉再开"
    // 的新仓错认成老队列。controller 的 pair 节点不带 open_date,所以在服务端就地
    // 用 inventory(只读)解析好,前端只拿现成倍数,不重复实现队列逻辑。
    const readState = (f: string) => {
      const q = path.join(REPO, 'trading_quantconnect', 'state', f);
      if (!fs.existsSync(q)) return null;
      return JSON.parse(fs.readFileSync(q, 'utf-8'));
    };
    const L = readState('legacy_positions.json')?.frozen ?? null;
    const S = readState('scaled_positions.json')?.frozen ?? null;
    const cohorts: Record<string, Record<string, { cohort: string; m: number }>> = {};
    for (const st of ['mrpt', 'mtfs']) {
      const inv = path.join(REPO, `inventory_${st}.json`);
      if (!fs.existsSync(inv)) continue;
      const pairs = JSON.parse(fs.readFileSync(inv, 'utf-8')).pairs ?? {};
      const key = (a: any) => `${a.pair}@${a.open_date}`;
      const lset = new Set((L?.[st] ?? []).map(key));
      const sset = new Set((S?.[st] ?? []).map(key));
      const k = Number(s.scalars[st]);
      const out: Record<string, { cohort: string; m: number }> = {};
      for (const [name, v] of Object.entries<any>(pairs)) {
        if (!v?.direction) continue;
        const id = `${name}@${v.open_date}`;
        out[name] = lset.has(id) ? { cohort: 'L', m: 0 }
          : sset.has(id) ? { cohort: 'S', m: k }
            : { cohort: 'F', m: 1 };
      }
      cohorts[st] = out;
    }
    res.json({
      scalars: s.scalars,
      frozen_at: s.scalar_basis?.frozen_at ?? null,
      capital_base: capitalBase,
      // S 冻结集尚未建立(还没跑 --freeze-scaled)时 scaled=null → 全部非 legacy 仓
      // 会被报成 F(m=1)。前端据此显示告警,而不是默默画一组错股数。
      scaled_frozen: S !== null,
      cohorts,
      // roll-off:L/S 两队清空且 QC 逐票收敛后由 ops/rolloff.py 一次性测定并冻结的
      // 净值层常数 K —— 此后 QC 净值 + K ≡ 面板净值。未到那天为 null。
      rolloff: readState('rolloff.json'),
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// 最近一次对账报告(看板头部 vs official 徽标)
router.get('/reconcile', (_req, res) => {
  if (!fs.existsSync(OUT)) return res.status(404).json({ error: 'no output dir' });
  const files = fs.readdirSync(OUT).filter(f => f.startsWith('reconcile_')).sort();
  if (!files.length) return res.json({ verdict: 'none' });
  res.json(JSON.parse(fs.readFileSync(path.join(OUT, files[files.length - 1]), 'utf-8')));
});

export default router;
