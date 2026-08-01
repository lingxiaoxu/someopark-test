"""StrategyMode.py — ±30% 获利了结/等待反弹 模式状态机(Post-Crash P6,2026-08-01)。

设计依据: .claude/plans/POST_CRASH_ADAPTIVITY_PLAN.md §6(文献综述+触发线论证)。
文献锚: Cooper/Gutierrez/Hameed 2004(市场态依赖)、Daniel&Moskowitz 2016(崩盘后
loser反弹)、Garg et al. 2021(转折期快动量)、Han/Zhou/Zhu 2016(组合级止损)、
Grossman&Zhou 1993(drawdown 控制)。equity触发是市场态的滞后代理 → 双条件:
equity 变动 ∧ 市场态确认,避免纯 equity 触发的噪音。

三态: NORMAL / REBOUND_HUNT(暴跌后抢反弹) / PROFIT_LOCK(暴涨后锁盈)。
- REBOUND_HUNT: 60td 内 equity 距峰 ≤ -30% ∧ 市场非瀑布中
  (VIX ≤ 0.8×60d峰 已释放 OR VIX < 22 绝对平静——2026-07 校准: 该次动量内爆
   VIX 峰仅 ~19-22,纯"释放"条款会把入场拖过反弹主段;绝对平静条款兜住
   "无恐慌型"崩盘,而 2020 式瀑布(VIX 攀升&>22)两条款都不满足 ✓)
- PROFIT_LOCK: 60td 内 equity 距谷 ≥ +30%(equity-only;锁盈是低风险动作)
- 驻留 ≤40td 强制回 NORMAL;退出后 10td 冷却防振荡;两触发同时满足时
  取"更近的极值"一侧。

PIT 纪律: 一切历史读取严格 < signal_date(与 DailySignal circuit-breaker 同款)。
状态持久化: trading_signals/strategy_mode_state.json(原子写);测试用
STRATEGY_MODE_STATE_DIR 环境变量重定向,绝不碰生产状态。
dry-run 语义: detect 纯函数;save_state 由调用方仅在非 dry_run 时调用。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

import numpy as np
import pandas as pd

log = logging.getLogger("StrategyMode")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = os.environ.get("STRATEGY_MODE_STATE_DIR",
                            os.path.join(BASE_DIR, "trading_signals"))
STATE_FILE = os.path.join(_STATE_DIR, "strategy_mode_state.json")

MODES = ("NORMAL", "REBOUND_HUNT", "PROFIT_LOCK")
LOOKBACK_TD = 60          # 滚动峰/谷回看交易日
DRAW_TRIG = -0.30         # 峰→现 ≤ -30% → REBOUND_HUNT 候选
RALLY_TRIG = +0.30        # 谷→现 ≥ +30% → PROFIT_LOCK 候选
MODE_MAX_TD = 40          # 模式最长驻留(交易日)
COOLDOWN_TD = 10          # 退出后冷却(交易日)
VIX_RELEASE_FRAC = 0.8    # VIX ≤ 0.8×60d峰 = 已释放
VIX_CALM_ABS = 22.0       # VIX 绝对平静线(2026-07 校准,见模块 docstring)


# ── 状态持久化 ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — 无状态文件 = 全 NORMAL 起步
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, STATE_FILE)


# ── 数据装载(PIT) ────────────────────────────────────────────────────────────

def _load_perf_rows(signal_date: str) -> list[dict]:
    """strategy_performance.json 行,严格 < signal_date。"""
    p = os.path.join(BASE_DIR, "someo-park-investment-management",
                     "public", "data", "strategy_performance.json")
    with open(p) as f:
        rows = json.load(f)
    return [r for r in rows if str(r.get("date", "")) < signal_date]


def _load_vix(signal_date: str) -> pd.Series:
    """VIX 日序列,严格 < signal_date(MacroStateStore 周表,只读)。"""
    from MacroStateStore import MacroStateStore
    start = (pd.Timestamp(signal_date) - pd.Timedelta(days=140)).strftime("%Y-%m-%d")
    df = MacroStateStore().load(start, signal_date)
    s = df["vix"].dropna()
    return s[s.index < pd.Timestamp(signal_date)]


# ── 核心: 纯函数 detect ──────────────────────────────────────────────────────

def detect(strategy: str, signal_date: str,
           perf_rows: list[dict] | None = None,
           vix: pd.Series | None = None,
           prior_state: dict | None = None) -> dict:
    """返回 {'mode','entered','days_in','trigger_detail','state'}。

    state = 应持久化的新状态 dict(调用方非 dry_run 时 save_state)。
    数据不足/异常 → NORMAL(fail-open,大声 log)。
    """
    sd = str(signal_date)
    try:
        rows = perf_rows if perf_rows is not None else _load_perf_rows(sd)
        vix_s = vix if vix is not None else _load_vix(sd)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[MODE] {strategy}: 数据装载失败 → NORMAL ({e})")
        return {"mode": "NORMAL", "entered": None, "days_in": 0,
                "trigger_detail": f"data unavailable: {e}", "state": prior_state or {}}

    st_all = dict(prior_state if prior_state is not None else load_state())
    st = dict(st_all.get(strategy, {}))

    eq_col = f"{strategy}_equity"
    eq = [(r["date"], float(r[eq_col])) for r in rows
          if r.get(eq_col) is not None][-LOOKBACK_TD:]
    if len(eq) < 20 or len(vix_s) < 20:
        return {"mode": "NORMAL", "entered": None, "days_in": 0,
                "trigger_detail": f"insufficient history (eq={len(eq)}, vix={len(vix_s)})",
                "state": st_all}

    dates = [d for d, _ in eq]
    vals = np.array([v for _, v in eq])
    cur = vals[-1]
    i_peak = int(np.argmax(vals)); i_trough = int(np.argmin(vals))
    peak, trough = vals[i_peak], vals[i_trough]
    draw = cur / peak - 1.0 if peak > 0 else 0.0
    rally = cur / trough - 1.0 if trough > 0 else 0.0

    vix_now = float(vix_s.iloc[-1])
    vix_peak60 = float(vix_s.tail(LOOKBACK_TD).max())
    vix_ok = (vix_now <= VIX_RELEASE_FRAC * vix_peak60) or (vix_now < VIX_CALM_ABS)

    # ── 在模式中: 对侧触发可中断 > 驻留到期 > 维持 ──
    # 对侧中断(2026-08-01 历史回放校准): 6/12 锁盈后 7 月崩 -42%,若锁死在
    # PROFIT_LOCK 到期,反弹期还挂着"更难进场"的 overlay = 反效果。对侧条件
    # 满足时直接切换(带各自的市场确认),同侧重入才受冷却约束。
    cur_mode = st.get("mode", "NORMAL")
    if cur_mode in ("REBOUND_HUNT", "PROFIT_LOCK"):
        days_in = int(np.busday_count(st["entered"], sd))
        opposite = None
        if cur_mode == "PROFIT_LOCK" and draw <= DRAW_TRIG and vix_ok:
            opposite = ("REBOUND_HUNT",
                        f"PL中断: eq {draw:+.1%} off peak, VIX {vix_now:.1f} ok")
        elif cur_mode == "REBOUND_HUNT" and rally >= RALLY_TRIG:
            opposite = ("PROFIT_LOCK", f"RH中断: eq {rally:+.1%} off trough")
        if opposite:
            new_mode, detail = opposite
            st = {"mode": new_mode, "entered": sd, "trigger_detail": detail}
            st_all[strategy] = st
            log.info(f"[MODE] {strategy.upper()}: {cur_mode} → {new_mode} ({detail})")
            return {"mode": new_mode, "entered": sd, "days_in": 0,
                    "trigger_detail": detail, "state": st_all}
        if days_in >= MODE_MAX_TD:
            st = {"mode": "NORMAL", "entered": None,
                  "cooldown_until": str(np.busday_offset(sd, COOLDOWN_TD,
                                                         roll="forward").astype(str))}
            st_all[strategy] = st
            return {"mode": "NORMAL", "entered": None, "days_in": 0,
                    "trigger_detail": f"{cur_mode} expired after {days_in}td → cooldown",
                    "state": st_all}
        st_all[strategy] = st
        return {"mode": cur_mode, "entered": st["entered"], "days_in": days_in,
                "trigger_detail": st.get("trigger_detail", ""), "state": st_all}

    # ── NORMAL: 冷却检查 ──
    cd = st.get("cooldown_until")
    if cd and sd < cd:
        st_all[strategy] = st
        return {"mode": "NORMAL", "entered": None, "days_in": 0,
                "trigger_detail": f"cooldown until {cd}", "state": st_all}

    # ── 触发判定(双满足取更近极值一侧) ──
    cand = None
    if draw <= DRAW_TRIG and rally >= RALLY_TRIG:
        cand = "REBOUND_HUNT" if i_peak > i_trough else "PROFIT_LOCK"
    elif draw <= DRAW_TRIG:
        cand = "REBOUND_HUNT"
    elif rally >= RALLY_TRIG:
        cand = "PROFIT_LOCK"

    if cand == "REBOUND_HUNT" and not vix_ok:
        st_all[strategy] = st
        return {"mode": "NORMAL", "entered": None, "days_in": 0,
                "trigger_detail": (f"draw {draw:+.1%} 触发但市场未确认"
                                   f"(VIX {vix_now:.1f} > {VIX_RELEASE_FRAC}×峰{vix_peak60:.1f}"
                                   f" 且 ≥ {VIX_CALM_ABS}) — 仍在瀑布中"),
                "state": st_all}

    if cand:
        detail = (f"eq {draw:+.1%} off {dates[i_peak]} peak, VIX {vix_now:.1f} ok"
                  if cand == "REBOUND_HUNT" else
                  f"eq {rally:+.1%} off {dates[i_trough]} trough")
        st = {"mode": cand, "entered": sd, "trigger_detail": detail}
        st_all[strategy] = st
        log.info(f"[MODE] {strategy.upper()}: NORMAL → {cand} ({detail})")
        return {"mode": cand, "entered": sd, "days_in": 0,
                "trigger_detail": detail, "state": st_all}

    st_all[strategy] = st if st else {"mode": "NORMAL"}
    return {"mode": "NORMAL", "entered": None, "days_in": 0,
            "trigger_detail": f"draw {draw:+.1%} / rally {rally:+.1%} (未触发)",
            "state": st_all}
