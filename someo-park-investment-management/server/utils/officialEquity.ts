// server/utils/officialEquity.ts — 官方 EOD 权益锚(策略组合口径)的单一真源
// 锚定映射与 controller/reconcile_eod.py 的 _ANCHORS 一致(原始源,只读):
//   pairs 族 MRPT/MTFS  → public/data/strategy_performance.json        (mrpt_equity / mtfs_equity)
//   qlib 族 SSRS/AISS/AEUS → public/data/master_portfolio_performance.json (sr_equity / aiss_equity / aeus_equity)
//   BDC                → public/data/private_credit_bdc_performance.json (bdc_equity)
// controller-nav /official(实时净值面板主数字)与 VolumePrediction 面板的 AUM 卡
// 都从这里取 —— "策略组合口径" 只允许有一个定义。账本口径(account_*.json 的
// equity)与之差一个 go-live 冻结常数(乘性族 ×k / 加性族 −C),不能混用。
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA = path.join(__dirname, '..', '..', 'public', 'data');

export type EquityPoint = { date: string; value: number };

function readLast(file: string, cols: string[]): Record<string, EquityPoint> {
  const rows = JSON.parse(fs.readFileSync(path.join(DATA, file), 'utf-8'));
  const out: Record<string, EquityPoint> = {};
  for (const col of cols) {
    for (let i = rows.length - 1; i >= 0; i--) {
      if (rows[i][col] != null) { out[col] = { date: rows[i].date, value: rows[i][col] }; break; }
    }
  }
  return out;
}

/** 六策略最新官方 EOD 权益(缺文件/缺列的策略为 undefined,不抛)。 */
export function readOfficialEquity(): Record<string, EquityPoint | undefined> {
  const safe = (file: string, cols: string[]) => {
    try { return readLast(file, cols); } catch { return {} as Record<string, EquityPoint>; }
  };
  const sp = safe('strategy_performance.json', ['mrpt_equity', 'mtfs_equity']);
  const mp = safe('master_portfolio_performance.json', ['sr_equity', 'aiss_equity', 'aeus_equity']);
  const bd = safe('private_credit_bdc_performance.json', ['bdc_equity']);
  return {
    mrpt: sp.mrpt_equity, mtfs: sp.mtfs_equity,
    ssrs: mp.sr_equity, aiss: mp.aiss_equity, aeus: mp.aeus_equity, bdc: bd.bdc_equity,
  };
}
