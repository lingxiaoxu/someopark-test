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

// 最新全层级值(前端 45s 轮询)
router.get('/latest', (_req, res) => {
  const p = path.join(OUT, 'nav_latest.json');
  if (!fs.existsSync(p)) return res.status(404).json({ error: 'controller not running yet' });
  res.json(JSON.parse(fs.readFileSync(p, 'utf-8')));
});

// 当日分钟流(频率子采样在前端做;date=YYYYMMDD 默认今天;
// date=latest → 最近一个有数据(≥2 行)的交易时段,闭市回看用)
router.get('/stream', (req, res) => {
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
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

// 前一交易日 controller 收盘值(最近一个 date<今天 的 nav_stream,
// 取 16:00 ET 截断前的末笔——与官方 EOD 同时点对齐;闭市 carry 行不算收盘)
function et16utcMs(d8: string): number {
  const y = +d8.slice(0, 4), m = +d8.slice(4, 6), d = +d8.slice(6, 8);
  for (const h of [20, 21]) {                 // EDT=UTC-4 → 20:00Z;EST=UTC-5 → 21:00Z
    const t = Date.UTC(y, m - 1, d, h, 0, 0);
    const etH = new Intl.DateTimeFormat('en-US',
      { timeZone: 'America/New_York', hour: '2-digit', hour12: false }).format(new Date(t));
    if (+etH === 16) return t;
  }
  return Date.UTC(y, m - 1, d, 21, 0, 0);
}
router.get('/prev-close', (_req, res) => {
  if (!fs.existsSync(OUT)) return res.status(404).json({ error: 'no output dir' });
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const days = fs.readdirSync(OUT)
    .map(f => /^nav_stream_(\d{8})\.csv$/.exec(f)?.[1])
    .filter((d): d is string => !!d && d < today)
    .sort();
  if (!days.length) return res.json({ date: null, values: {} });
  const date = days[days.length - 1];
  const cutoff = et16utcMs(date);
  const values: Record<string, number> = {};
  for (const r of readStreamMerged(date)) {   // 顺序扫描,≤16:00 ET 的末笔覆盖 = 收盘
    if (Date.parse(String(r.ts)) > cutoff) continue;
    values[String(r.node_id)] = Number(r.value);
  }
  res.json({ date, values });
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

// 最近一次对账报告(看板头部 vs official 徽标)
router.get('/reconcile', (_req, res) => {
  if (!fs.existsSync(OUT)) return res.status(404).json({ error: 'no output dir' });
  const files = fs.readdirSync(OUT).filter(f => f.startsWith('reconcile_')).sort();
  if (!files.length) return res.json({ verdict: 'none' });
  res.json(JSON.parse(fs.readFileSync(path.join(OUT, files[files.length - 1]), 'utf-8')));
});

export default router;
