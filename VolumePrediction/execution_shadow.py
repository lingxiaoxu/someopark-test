"""execution_shadow — E1-2 执行层消费影子(2026-08-17 用户批准开发;影子先行)。

目的: 把四策略**真实发生的调仓单**每日喂给已建成的执行层
(execute.schedule / leg_joint.schedule_pair / basket.schedule_basket +
econ 成本模型),记录"执行层本会给出的排程与成本预算",为逐策略批准
真实切换积累证据 —— 与 W3/W4 wiring 影子同款纪律: 零接触策略代码,
只读策略输出,只写 VolumePrediction/outputs/execution_shadow/。

数据源(全部只读,解析规则与 tca_backfill 同源):
  MRPT/MTFS — combined_signals_*.json 的 signals 列表,对级事件
              (OPEN_LONG/OPEN_SHORT/CLOSE/CLOSE_STOP,s1/s2 股数+价格)
  AISS/SSRS — trade_ledger_{strat}.jsonl 的 BUY/SELL,按日聚合成篮子

排程语义(全部复用现存实现,不新造求解器):
  pairs → leg_joint.schedule_pair(对冲保持,逐日公共比例,参与率较紧腿约束;
          entry/exit/stop 共用 —— 该实现即 cap 驱动,stop 的 urgent 语义
          天然满足"最快完成",记 scheduler=leg_joint_cap 以示诚实)
  aiss  → basket.schedule_basket(align=aligned;profile constraints.basket=true)
  ssrs  → basket.schedule_basket(align=independent)
  μ/profile → econ.objective.resolve + resolve_mu(与生产同一注册表)

证据口径: 每笔记录排程天数/是否完成/瓶颈腿/最大参与率/排程总冲击成本
est_cost_sched,并与"单日打满"对照 est_cost_1d(同一 λ 口径)——
saving = cost_1d − cost_sched 即执行层的经济价值主证据。

PIT: 事件价用信号/成交价(决策时点);ADV̂ 用 svc.adv.info(date=事件日)
(history 工件,前瞻多日 v1 平推,与 execute._adv_forecast_series 同约定)。

幂等: tracking CSV 按 key=(strategy,date,unit,action) 去重,重复运行不灌重。

用法:
    python -m VolumePrediction.execution_shadow --dry-run
    python -m VolumePrediction.execution_shadow            # 增量(默认 since 2026-08-01)
    python -m VolumePrediction.execution_shadow --since 2026-08-10
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from VolumePrediction.common import OUT, REPO, get_logger, sanitize_for_json

log = get_logger("execution_shadow")

SHADOW_DIR = OUT / "execution_shadow"
TRACKING = SHADOW_DIR / "exec_shadow_tracking.csv"
SIGNALS_GLOB = str(REPO / "trading_signals" / "combined_signals_*.json")
LEDGERS = {
    "aiss": REPO / "qlib-main" / "semiconductor_strategy" / "trade_ledger_aiss.jsonl",
    "ssrs": REPO / "qlib-main" / "sector_rotation" / "trade_ledger_ssrs.jsonl",
}
PAIR_ACTIONS = ("OPEN_LONG", "OPEN_SHORT", "CLOSE", "CLOSE_STOP")
_TRADE_TYPE = {"OPEN_LONG": "entry", "OPEN_SHORT": "entry",
               "CLOSE": "exit", "CLOSE_STOP": "stop"}
DEFAULT_SINCE = "2026-08-01"        # forecast history 工件稳定起点(promote 后)
MAX_DAYS = 10


# ── 事件收集(对级/篮级;解析规则与 tca_backfill._from_pairs_signals 同源)──

def collect_pair_trades(since: str) -> list[dict]:
    out, seen = [], set()
    for f in sorted(glob.glob(SIGNALS_GLOB)):
        try:
            d = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        sd = d.get("signal_date", "")
        if not sd or sd < since:
            continue
        for strat in ("mrpt", "mtfs"):
            for s in (d.get(strat, {}) or {}).get("signals", []):
                act = s.get("action")
                if act not in PAIR_ACTIONS:
                    continue
                key = (strat, sd, s.get("pair"), act)
                if key in seen:
                    continue
                seen.add(key)
                legs_ok = all(
                    s.get(k) not in (None, 0) for k in
                    ("s1", "s2", "s1_shares", "s2_shares", "s1_price", "s2_price"))
                out.append({
                    "strategy": strat, "date": sd, "unit": s.get("pair"),
                    "action": act, "trade_type": _TRADE_TYPE[act],
                    "s1": s.get("s1"), "s2": s.get("s2"),
                    "s1_shares": s.get("s1_shares"), "s2_shares": s.get("s2_shares"),
                    "s1_price": s.get("s1_price"), "s2_price": s.get("s2_price"),
                    "legs_ok": legs_ok})
    return out


def collect_basket_trades(since: str) -> list[dict]:
    """账本 BUY/SELL 按 (策略,日) 聚合成篮子;shares 带方向。"""
    by_day: dict[tuple, dict] = {}
    for strat, p in LEDGERS.items():
        if not p.exists():
            log.warning(f"账本缺失: {p}")
            continue
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("side") not in ("BUY", "SELL"):
                continue
            dt, tk = r.get("date"), r.get("ticker")
            sh = float(r.get("shares", 0) or 0)
            px = float(r.get("price", 0) or 0)
            if not dt or dt < since or not tk or sh <= 0 or px <= 0:
                continue
            k = (strat, dt)
            b = by_day.setdefault(k, {"strategy": strat, "date": dt,
                                      "unit": "basket", "action": "REBALANCE",
                                      "trade_type": "rebalance",
                                      "trades": {}, "prices": {}})
            signed = sh if r["side"] == "BUY" else -sh
            b["trades"][tk] = b["trades"].get(tk, 0.0) + signed
            b["prices"][tk] = px                       # 同日多笔取末价(成交价)
    out = []
    for b in by_day.values():
        b["trades"] = {t: s for t, s in b["trades"].items() if abs(s) > 1e-9}
        if b["trades"]:
            out.append(b)
    return out


# ── 排程 + 成本证据 ──────────────────────────────────────────────────────────

def _adv_series(svc, ticker: str, date: str, days: int = MAX_DAYS
                ) -> tuple[Optional[pd.Series], Optional[float], str]:
    """(未来 days 工作日预测股量(v1 平推), PIT last_price, source)。
    与 service._Execute._adv_forecast_series 同约定,date 显式必传保 PIT。"""
    info = svc.adv.info(ticker, date=date)
    shares = info.get("adv_shares")
    if shares is None:
        return None, None, "unavailable"
    idx = pd.bdate_range(pd.Timestamp(date) + pd.Timedelta(days=1), periods=days)
    return (pd.Series(float(shares), index=idx), info.get("last_price"),
            info.get("source", "?"))


def _profile_mu(strategy: str, trade_type: str):
    from VolumePrediction.econ import objective as obj
    prof = obj.resolve(strategy=strategy, trade_type=trade_type)
    mu, mu_src = obj.resolve_mu(prof)
    return prof, mu, mu_src


def shadow_pair(svc, ev: dict) -> dict:
    from VolumePrediction.econ.policy import impact_cost_dollars
    from VolumePrediction.execution.leg_joint import schedule_pair
    rec = {k: ev[k] for k in ("strategy", "date", "unit", "action", "trade_type")}
    rec["scheduler"] = "leg_joint_cap"
    if not ev["legs_ok"]:
        rec["status"] = "no_shares_in_signal"       # 注入模拟等边角,如实记录
        return rec
    prof, mu, mu_src = _profile_mu(ev["strategy"], ev["trade_type"])
    a1, _, src1 = _adv_series(svc, ev["s1"], ev["date"])
    a2, _, src2 = _adv_series(svc, ev["s2"], ev["date"])
    rec.update({"profile": prof.name, "mu": mu, "mu_source": mu_src,
                "adv_source": {ev["s1"]: src1, ev["s2"]: src2}})
    if a1 is None or a2 is None:
        rec["status"] = "no_adv_data"
        return rec
    p1, p2 = float(ev["s1_price"]), float(ev["s2_price"])
    t1, t2 = abs(float(ev["s1_shares"])), abs(float(ev["s2_shares"]))
    df = schedule_pair(ev["s1"], ev["s2"], t1, t2, p1, p2, a1, a2,
                       participation_cap=prof.adv_cap, max_days=MAX_DAYS)
    cost_1d = (impact_cost_dollars(t1 * p1, float(a1.iloc[0]) * p1)
               + impact_cost_dollars(t2 * p2, float(a2.iloc[0]) * p2))
    active = df[df["frac"] > 0] if len(df) else df
    rec.update({
        "status": "ok",
        "notional": t1 * p1 + t2 * p2,
        "days_scheduled": int(len(active)),
        "completed": bool(df.attrs.get("completed", False)),
        "remaining_frac": float(df.attrs.get("remaining_frac", 0.0)),
        "bottleneck_leg": df.attrs.get("bottleneck_leg"),
        "max_participation": float(
            pd.concat([df["s1_participation"], df["s2_participation"]]).max())
        if len(df) else None,
        "est_cost_sched": float(df["est_cost_dollars"].sum()) if len(df) else 0.0,
        "est_cost_1d": float(cost_1d),
        "schedule": [{"date": str(r["date"].date() if hasattr(r["date"], "date")
                                  else r["date"]),
                      "frac": round(float(r["frac"]), 6),
                      "s1_shares": round(float(r["s1_shares"]), 2),
                      "s2_shares": round(float(r["s2_shares"]), 2)}
                     for _, r in active.iterrows()]})
    rec["est_saving"] = rec["est_cost_1d"] - rec["est_cost_sched"]
    return rec


def shadow_basket(svc, ev: dict) -> dict:
    from VolumePrediction.econ.policy import impact_cost_dollars
    from VolumePrediction.execution.basket import schedule_basket
    rec = {k: ev[k] for k in ("strategy", "date", "unit", "action", "trade_type")}
    prof, mu, mu_src = _profile_mu(ev["strategy"], ev["trade_type"])
    align = "aligned" if prof.constraints.get("basket") else "independent"
    rec.update({"profile": prof.name, "mu": mu, "mu_source": mu_src,
                "scheduler": f"basket_{align}"})
    advs, missing = {}, []
    for tk in ev["trades"]:
        s, _, src = _adv_series(svc, tk, ev["date"])
        if s is None:
            missing.append(tk)
        else:
            advs[tk] = s
    if missing:
        rec["no_adv_tickers"] = missing
    trades = [{"ticker": tk, "shares": sh} for tk, sh in ev["trades"].items()
              if tk in advs]
    if not trades:
        rec["status"] = "no_adv_data"
        return rec
    res = schedule_basket(trades, prices=ev["prices"], adv_forecasts=advs,
                          profile=prof, mu=mu, max_days=MAX_DAYS, align=align)
    summ = res["summary"]
    cost_1d = sum(
        impact_cost_dollars(abs(sh) * ev["prices"][tk],
                            float(advs[tk].iloc[0]) * ev["prices"][tk])
        for tk, sh in ev["trades"].items() if tk in advs)
    rec.update({
        "status": "ok" if not missing else "partial",
        "n_tickers": len(trades),
        "notional": float(sum(abs(sh) * ev["prices"][tk]
                              for tk, sh in ev["trades"].items() if tk in advs)),
        "days_scheduled": int(summ["days"].max()) if len(summ) else 0,
        "completed": bool(summ["completed"].all()) if len(summ) else False,
        "max_participation": float(summ["max_participation"].max()) if len(summ) else None,
        "est_cost_sched": float(summ["est_cost_dollars"].sum()) if len(summ) else 0.0,
        "est_cost_1d": float(cost_1d),
        "per_ticker": summ.to_dict(orient="records")})
    rec["est_saving"] = rec["est_cost_1d"] - rec["est_cost_sched"]
    return rec


# ── 主流程(幂等增量)─────────────────────────────────────────────────────────

_TRACK_COLS = ["strategy", "date", "unit", "action", "trade_type", "status",
               "profile", "mu_source", "notional", "days_scheduled", "completed",
               "max_participation", "est_cost_sched", "est_cost_1d", "est_saving"]


def _done_keys() -> set:
    if not TRACKING.exists():
        return set()
    t = pd.read_csv(TRACKING, dtype=str)
    return {(r["strategy"], r["date"], r["unit"], r["action"])
            for _, r in t.iterrows()}


def run(since: str = DEFAULT_SINCE, dry_run: bool = False) -> dict:
    events = collect_pair_trades(since) + collect_basket_trades(since)
    done = _done_keys()
    todo = [e for e in events
            if (e["strategy"], e["date"], e["unit"], e["action"]) not in done]
    log.info(f"execution_shadow: {len(events)} events since {since}, "
             f"{len(todo)} new, {len(done)} already tracked")
    if dry_run or not todo:
        return {"status": "ok", "events": len(events), "new": len(todo),
                "dry_run": dry_run}

    from VolumePrediction.service import VolumeService
    svc = VolumeService()
    recs: list[dict] = []
    by_day: dict[tuple, list] = {}
    for ev in todo:
        rec = shadow_pair(svc, ev) if "s1" in ev else shadow_basket(svc, ev)
        recs.append(rec)
        by_day.setdefault((rec["strategy"], rec["date"]), []).append(rec)
        log.info(f"  [{rec['strategy']}] {rec['date']} {rec['unit']} "
                 f"{rec['action']} → {rec['status']}"
                 + (f" sched {rec.get('days_scheduled')}d "
                    f"cost {rec.get('est_cost_sched', 0):.0f} vs 1d "
                    f"{rec.get('est_cost_1d', 0):.0f}"
                    if rec.get("status") in ("ok", "partial") else ""))

    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    for (strat, dt), rows in by_day.items():
        p = SHADOW_DIR / f"exec_shadow_{strat}_{dt}.json"
        existing = []
        if p.exists():
            try:
                existing = json.loads(p.read_text()).get("trades", [])
            except Exception:  # noqa: BLE001
                pass
        doc = {"schema_version": "v1", "strategy": strat, "date": dt,
               "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
               "trades": existing + rows}
        doc, _bad = sanitize_for_json(doc)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False,
                                  default=str, allow_nan=False))
        tmp.replace(p)

    track_rows = [{c: r.get(c) for c in _TRACK_COLS} for r in recs]
    tdf = pd.DataFrame(track_rows)
    header = not TRACKING.exists()
    tdf.to_csv(TRACKING, mode="a", header=header, index=False)
    n_ok = sum(1 for r in recs if r.get("status") in ("ok", "partial"))
    log.info(f"execution_shadow: +{len(recs)} rows ({n_ok} scheduled) "
             f"→ {TRACKING.name}")
    return {"status": "ok", "events": len(events), "new": len(recs),
            "scheduled": n_ok}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        r = run(since=a.since, dry_run=a.dry_run)
    except Exception as e:  # noqa: BLE001 — 结构性失败大声退出,绝不静默
        log.error(f"execution_shadow FAILED: {e}", exc_info=True)
        return 1
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
