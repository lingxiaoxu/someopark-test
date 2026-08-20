"""rolloff — 退场日:实测并冻结净值层常数 K,使 QC 净值 + K ≡ 面板净值。

背景(2026-08-19 三队列):pairs 的镜像倍数在开仓那一刻定死,永不改 ——
    L(go-live 冻结)  m = 0   QC 从未持有
    S(切换日冻结)    m = k   QC 已按 k 建过仓,保持不动
    F(切换后新开)    m = 1   全额镜像
L 与 S 都是**有限寿命**的:每对总会平仓,平了就永远不会回来(新开的落 F)。
两队清空的那一天 = 退场日。那之后 QC 的每一票都与官方口径持仓相同:
    pairs  → m=1,QC 股数 = 账本股数 = 官方股数(加性族 official = ledger − C,敞口 1:1)
    其余   → QC 股数 = 账本 × k = 官方股数(乘性族 official = ledger × k)
持仓既已逐票相同,两边净值之差就只剩现金项,不再随行情漂移 —— 它定格成常数 K。
把 K 记到 QC 一侧当现金,QC 净值 + K 就恒等于面板净值,两个口径从此收敛。

用法:
  python ops/rolloff.py --check      离线: 现在还剩几对没退场(不碰网络)
  python ops/rolloff.py --measure    只读: 逐票对拍 QC↔target + 试算 K(不写文件)
  python ops/rolloff.py --freeze     退场日一次性: 校验通过后写 state/rolloff.json

防火墙:本模块只读五个持仓文件、state/、public/data/*.json 与 QC 只读接口;
只写 state/rolloff.json。绝不写 inventory,绝不向 QC 下单。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_THIS_DIR))

from inventory_source import (PAIR_STRATEGIES, SourceError,   # noqa: E402
                              build_target, open_pairs, read_snapshot)

REPO = _THIS_DIR.parent
STATE_DIR = _THIS_DIR / "state"
LEGACY_PATH = STATE_DIR / "legacy_positions.json"
SCALED_PATH = STATE_DIR / "scaled_positions.json"
EXPORTER_STATE = STATE_DIR / "exporter_state.json"
TARGET_COPY = STATE_DIR / "target_portfolio.json"
ROLLOFF_PATH = STATE_DIR / "rolloff.json"
DATA = REPO / "someo-park-investment-management" / "public" / "data"

# 官方 EOD 净值:每个策略取自哪份已发布的 performance json 的哪个字段
OFFICIAL_FIELDS = {
    "mrpt": ("strategy_performance.json", "mrpt_equity"),
    "mtfs": ("strategy_performance.json", "mtfs_equity"),
    "ssrs": ("master_portfolio_performance.json", "sr_equity"),
    "aiss": ("master_portfolio_performance.json", "aiss_equity"),
    "bdc": ("private_credit_bdc_performance.json", "bdc_equity"),
}
# 逐票对拍允许的股数偏差:0。QC 是整数股镜像,差 1 股就说明没收敛(或有单在飞)。
TOL_SHARES = 0
# runtimeStatistics.Equity 与 (持仓市值 + 现金) 是**两个不同时刻的采样**(前者随
# 图表节流更新),盘中差几万美元是正常的 —— 8/19 23:4x 实测差 $32,854 ≈ 0.57%。
# 所以它不是"对不对"的判据,而是"此刻静不静"的判据:收盘后两次采样应当几乎重合。
# K 一旦冻结就是永久常数,必须在静止时刻测,故 --freeze 要求这个差 ≤ TOL_QUIET。
TOL_QUIET = 500.0


def _load(p: Path, default=None):
    return json.loads(p.read_text()) if p.exists() else default


def _atomic_write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.replace(p)


def alive_queues() -> dict:
    """离线重算 L / S 两队还活着的对。

    **不能**读 target_portfolio.json 里的 legacy_alive —— legacy 对平仓不改
    target 哈希(QC 零动作),export_once 会提前 return,那份快照因此可能是陈的。
    退场判定必须自己按当前 inventory 现算。
    """
    snap = read_snapshot()
    legacy = _load(LEGACY_PATH)
    scaled = _load(SCALED_PATH)
    if legacy is None or scaled is None:
        raise SourceError("缺 legacy_positions.json 或 scaled_positions.json — "
                          "先 --golive / --freeze-scaled")
    built = build_target(snap, legacy=legacy.get("frozen"),
                         scalars=_load(EXPORTER_STATE, {}).get("scalars") or {},
                         scaled=scaled.get("frozen"))
    today = date.today()
    out = {}
    for st in PAIR_STRATEGIES:
        rows = []
        for q, items in (("L", built["legacy_alive"].get(st, [])),
                         ("S", built["scaled_alive"].get(st, []))):
            for it in items:
                od = it.get("open_date")
                age = (today - date.fromisoformat(od)).days if od else None
                rows.append({"queue": q, "pair": it["pair"],
                             "open_date": od, "age_days": age})
        out[st] = sorted(rows, key=lambda r: (r["queue"], r["open_date"] or ""))
    return out


def official_eod() -> tuple[str, dict[str, float]]:
    """已发布的官方 EOD 净值(五策略必须同一天,否则拒绝)。"""
    vals, dates = {}, {}
    for st, (fn, field) in OFFICIAL_FIELDS.items():
        rows = _load(DATA / fn)
        if not rows:
            raise SourceError(f"读不到 {fn}")
        last = rows[-1]
        if field not in last:
            raise SourceError(f"{fn} 末行缺字段 {field}")
        vals[st], dates[st] = float(last[field]), str(last["date"])
    if len(set(dates.values())) != 1:
        raise SourceError(f"五策略官方 EOD 日期不一致 {dates} —— 夜间 pipeline "
                          f"没跑齐就冻 K 会把一天的盈亏永久焊进常数里")
    return next(iter(dates.values())), vals


def qc_snapshot() -> dict:
    """QC 只读快照:逐票股数 + 持仓市值 + 现金 + 账户净值(自算与 QC 报的对拍)。"""
    from ops.deploy import ALGOS
    from qc_api import QcClient
    c = QcClient()
    pid = c.find_project(ALGOS["mirror"]["project"])
    if pid is None:
        raise SourceError("QC 上找不到 mirror 项目")
    lr = c.live_read(pid)
    if lr.get("status") != "Running":
        raise SourceError(f"mirror 不在 Running(status={lr.get('status')})")
    pf = c.live_portfolio(pid)["portfolio"]
    # holdings 的 key 是 "TICKER <QC内部ID>",取第一段
    holds = {k.split()[0]: v for k, v in (pf.get("holdings") or {}).items()}
    shares = {t: int(v.get("q") or 0) for t, v in holds.items()}
    mv = sum(float(v.get("v") or 0.0) for v in holds.values())
    cash = float(pf["cash"]["USD"]["amount"])
    reported = lr.get("runtimeStatistics", {}).get("Equity")
    eq_rep = float(str(reported).replace("$", "").replace(",", "")) if reported else None
    # 净值一律用同一份 payload 自算(持仓市值 + 现金),口径内部自洽;QC 自报值只留作
    # 静止判据与审计留痕,不参与 K 的计算。
    equity = mv + cash
    return {"shares": {t: s for t, s in shares.items() if s},
            "holdings_mv": mv, "cash": cash, "equity": equity,
            "equity_reported": eq_rep,
            "quiet_gap": None if eq_rep is None else eq_rep - equity,
            "deploy_id": lr.get("deployId")}


def _et_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


# QC 一侧的符号命名怪癖:QC 按 security ID 记仓,展示名取该 ID 的**历史首名**,
# 所以 2023 年 ORCC→OBDC 改名后的仓在 live/portfolio 里仍叫 ORCC,而 target 用
# 现名 OBDC。这不是持仓不符,是两边叫法不同 —— 不归一化就会让退场日永远冻不了 K。
# 只放在这里(而不是仓库根 ticker_aliases.json):它描述的是 QC 怎么命名,不是市场
# 事件,写进那份全仓库共用的映射会波及 VP/价格归一化等无关链路。
QC_SYMBOL_ALIAS = {"ORCC": "OBDC"}


def _canon(t: str) -> str:
    """QC 展示名 → 本仓库当前用名。先过 QC 侧怪癖表,再过仓库统一改名机制。"""
    t = QC_SYMBOL_ALIAS.get(t, t)
    try:
        sys.path.insert(0, str(REPO))
        from ticker_aliases import resolve
        return resolve(t)
    except Exception:                       # 改名表缺失不该拖垮对拍,原样返回
        return t


def convergence(qc_shares: dict[str, int]) -> tuple[list[tuple[str, int, int]], int]:
    """QC 实际持股 vs exporter 最新 target 的逐票差 → (差异表, 对拍票数)。"""
    tgt = _load(TARGET_COPY)
    if not tgt:
        raise SourceError("缺 state/target_portfolio.json")
    T = {_canon(t): int(v) for t, v in tgt["targets"].items()}
    Q: dict[str, int] = {}
    for t, s in qc_shares.items():
        Q[_canon(t)] = Q.get(_canon(t), 0) + s
    names = sorted(set(T) | set(Q))
    bad = [(t, Q.get(t, 0), T.get(t, 0)) for t in names
           if abs(Q.get(t, 0) - T.get(t, 0)) > TOL_SHARES]
    return bad, len(names)


def cmd_check(verbose: bool = True) -> bool:
    q = alive_queues()
    total = sum(len(v) for v in q.values())
    if verbose:
        for st, rows in q.items():
            if not rows:
                print(f"  {st}: 已清空 ✓")
                continue
            print(f"  {st}: 还剩 {len(rows)} 对")
            for r in rows:
                print(f"     [{r['queue']}] {r['pair']:<14} 开于 {r['open_date']}"
                      f"  已持 {r['age_days']} 天")
        print(f"\n退场条件(L+S 全空): {'满足 ✓' if total == 0 else f'未满足 — 还剩 {total} 对'}")
        if total:
            # 实测 235 段收敛寿命:MRPT 中位 1 天 / p95 5 天,MTFS 中位 5 天 / p95 15 天,
            # 100% 在 30 个日历日内平掉 —— 用最老一对的年龄给个粗略剩余窗口。
            oldest = max(r["age_days"] or 0 for rows in q.values() for r in rows)
            print(f"  最老一对已持 {oldest} 天;历史 100% 的仓在 30 个日历日内平掉,"
                  f"故预计还需 ≤ {max(0, 30 - oldest)} 天")
    return total == 0


def cmd_measure(freeze: bool = False) -> int:
    ready = cmd_check()
    print()
    qc = qc_snapshot()
    bad, n_all = convergence(qc["shares"])
    print(f"逐票对拍 QC ↔ target: {n_all - len(bad)}/{n_all} 相符")
    for t, a, b in bad:
        print(f"   {t:<6} QC {a:>8,}  target {b:>8,}  差 {a - b:+,}")
    d, off = official_eod()
    P = sum(off.values())
    K = P - qc["equity"]
    print(f"\n官方 EOD {d}: " + " ".join(f"{s}={v:,.0f}" for s, v in off.items()))
    print(f"  面板/官方口径合计 P = {P:>16,.2f}")
    print(f"  QC 账户净值      Q = {qc['equity']:>16,.2f}"
          f"   (持仓 {qc['holdings_mv']:,.2f} + 现金 {qc['cash']:,.2f})")
    print(f"  K = P − Q         = {K:>16,.2f}")
    gap = qc["quiet_gap"]
    if gap is not None:
        print(f"  静止判据: QC 自报 Equity {qc['equity_reported']:,.2f}"
              f" 与自算差 {gap:+,.2f}"
              f" ({'静止 ✓' if abs(gap) <= TOL_QUIET else '仍在动 — 盘中/有单在飞'})")
    if not freeze:
        prev = _load(ROLLOFF_PATH)
        if prev:
            drift = K - float(prev["k_equity"])
            print(f"\n已冻结 K = {float(prev['k_equity']):,.2f}"
                  f"(测于 {prev['measured_on']});今日实测偏离 {drift:+,.2f}"
                  f" —— 退场后残余漂移只应来自成交价差,长期缓慢累积。")
        return 0
    # ---- 以下只有 --freeze 会走到 ----
    if ROLLOFF_PATH.exists():
        raise SourceError(f"{ROLLOFF_PATH} 已存在 — K 只冻一次;确要重来先人工归档")
    if not ready:
        raise SourceError("L/S 两队未清空 —— 此刻的差还会随行情漂,冻下来就是个错常数")
    if bad:
        raise SourceError(f"QC 有 {len(bad)} 票未收敛到 target —— 先等镜像补齐再冻")
    et = _et_now()
    if not (et.hour >= 17 or et.hour < 4):
        raise SourceError(f"现在 {et:%H:%M} ET 属于盘中/盘后波动时段。K 是永久常数,"
                          f"必须在收盘静止后测(17:00–04:00 ET),否则两边不同时刻的"
                          f"标价差会被永久焊进常数里")
    if gap is not None and abs(gap) > TOL_QUIET:
        raise SourceError(f"QC 两个净值采样仍差 {gap:+,.2f}(阈值 ±{TOL_QUIET:,.0f})"
                          f" —— 账户还没静下来,等收盘结算完再冻")
    if d != et.strftime("%Y-%m-%d"):
        print(f"  注意: 官方 EOD 是 {d},不是今天({et:%Y-%m-%d} ET)"
              f" —— 确认这是最近一个已收盘交易日。")
    doc = {"frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "measured_on": d,
           "k_equity": round(K, 2),
           "panel_official_total": round(P, 2),
           "official_eod": {s: round(v, 2) for s, v in off.items()},
           "qc_equity": round(qc["equity"], 2),
           "qc_holdings_mv": round(qc["holdings_mv"], 2),
           "qc_cash": round(qc["cash"], 2),
           "qc_deploy_id": qc["deploy_id"],
           "n_tickers_matched": n_all,
           "note": "退场日实测:L/S 两队已清空且 QC 逐票收敛,两边持仓相同 ⇒ "
                   "净值差只剩现金项,定格为常数。QC 净值 + k_equity ≡ 面板净值。"}
    _atomic_write(ROLLOFF_PATH, doc)
    print(f"\n[rolloff] 已冻结 K = {K:,.2f} → {ROLLOFF_PATH}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="legacy/S 退场日 K 常数实测与冻结")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="离线:还剩几对没退场")
    g.add_argument("--measure", action="store_true", help="只读:对拍 + 试算 K")
    g.add_argument("--freeze", action="store_true", help="退场日一次性:冻结 K")
    a = ap.parse_args()
    try:
        if a.check:
            cmd_check()
            return 0
        return cmd_measure(freeze=a.freeze)
    except SourceError as e:
        print(f"!!!! [rolloff] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
