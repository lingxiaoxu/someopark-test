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

// 当日分钟流(频率子采样在前端做;date=YYYYMMDD 默认今天)
router.get('/stream', (req, res) => {
  const date = String(req.query.date || new Date().toISOString().slice(0, 10).replace(/-/g, ''));
  const p = path.join(OUT, `nav_stream_${date}.csv`);
  if (!fs.existsSync(p)) return res.json({ date, rows: [] });
  const lines = fs.readFileSync(p, 'utf-8').trim().split('\n');
  const header = lines[0].split(',');
  const rows = lines.slice(1).map(l => {
    const v = l.split(',');
    const o: Record<string, string | number> = {};
    header.forEach((h, i) => { o[h] = h === 'value' ? Number(v[i]) : v[i]; });
    return o;
  });
  res.json({ date, rows });
});

// 前一交易日 controller 收盘值(最近一个 date<今天 的 nav_stream 末笔;
// 日内 % 的开盘锚基准——含隔夜跳空,官方口径换算在前端做)
router.get('/prev-close', (_req, res) => {
  if (!fs.existsSync(OUT)) return res.status(404).json({ error: 'no output dir' });
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const days = fs.readdirSync(OUT)
    .map(f => /^nav_stream_(\d{8})\.csv$/.exec(f)?.[1])
    .filter((d): d is string => !!d && d < today)
    .sort();
  if (!days.length) return res.json({ date: null, values: {} });
  const date = days[days.length - 1];
  const lines = fs.readFileSync(path.join(OUT, `nav_stream_${date}.csv`), 'utf-8')
    .trim().split('\n');
  const header = lines[0].split(',');
  const iNode = header.indexOf('node_id'), iVal = header.indexOf('value');
  const values: Record<string, number> = {};
  for (const l of lines.slice(1)) {           // 顺序扫描,末笔覆盖 = 当日收盘
    const v = l.split(',');
    values[v[iNode]] = Number(v[iVal]);
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
