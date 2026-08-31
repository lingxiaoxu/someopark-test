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
    "aeus": ("master_portfolio_performance.json", "aeus_equity"),
    "bdc": ("private_credit_bdc_performance.json", "bdc_equity"),
}
# 逐票对拍允许的股数偏差:0。QC 是整数股镜像,差 1 股就说明没收敛(或有单在飞)。
TOL_SHARES = 0
# Q 与 QC 自报净值的交叉校验容差(gross 的 bp)。与 M4 的 CROSS_TOL_BP 同义同值。
#
# 这里原先是 TOL_QUIET = 500.0,注释说 runtimeStatistics.Equity 与"持仓市值 + 现金"
# 只是两个采样时刻不同、"收盘后两次采样应当几乎重合",并据此当"静不静"的判据。
# **那个前提是错的**(2026-08-28 实测,详见 reconcile/official_close.py 顶部):
# payload 里每票的 p 停在收盘前约 15 分钟,收盘后十小时都不再更新,所以自算净值
# 恒等于 15:45 那一刻的值,永远不会与收盘净值重合。8/27 差 27,979(48.5bp),
# 收盘后再静也是这个数。照原写法 --freeze 要么永远过不去,要么(把阈值放宽)
# 把少算的 2.8 万**永久焊进 K**。
#
# 现在 Q 改用"现金 + Σ 逐票股数 × 官方收盘价",与 P 同价源族;QC 自报净值降级为
# 独立交叉校验。自算净值仍留着,只作审计留痕(price_staleness_usd),不参与 K。
CROSS_TOL_BP = 3.0


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


def official_rows(p: Path) -> list:
    """读一份官方 EOD 序列文件。

    JSON 半截 → SourceError,不让 JSONDecodeError 裸奔:这三份文件由夜间 pipeline
    直接 json.dump 覆写(非原子),对账正好撞在写的当口就会读到截断的 JSON。
    那是"这一趟先别对,等下一趟"而不是"程序崩了",裸的 JSONDecodeError 会穿过
    equity_plane 的 SourceError 捕获,把整趟对账连同已经算好的 ①② 一起打掉。
    """
    if not p.exists():
        raise SourceError(f"读不到 {p.name}")
    try:
        rows = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise SourceError(f"{p.name} 不是完整 JSON({e})—— 多半正被夜间 "
                          f"pipeline 覆写,这一趟先别对") from e
    if not rows:
        raise SourceError(f"读不到 {p.name}")
    return rows


def official_eod(session: str | None = None) -> tuple[str, dict[str, float]]:
    """已发布的官方 EOD 净值(五策略必须同一天,否则拒绝)。

    session=None —— 取每份文件的**末行**,即"现在发布到哪天了"。--measure/--freeze
      问的正是这个:冻 K 要冻在最新那天。

    session 给定 —— **按日期查行**,不看末行。M4 补算必须走这条:session D 的 P
      只在 "P(D) 落地 → P(D+1) 落地" 之间当过末行(实测约 D+1 10:15 到 D+1 21:30),
      按末行取数等于把补算窗口硬压到不足一天 —— 连着两晚 pipeline 出问题,那两天
      的 equity 段就永久补不回来。
      按日期回查安全的前提是**三份文件都全历史重算重写**:strategy_performance
      每趟 `--start 2026-03-19` 起全段重算后整表落盘(DailySignal.py:3018);
      master 与 bdc 从头重建。所以 D 那一行会被 D 之后的每一趟收盘后跑批修正过
      —— 2026-08-27 BDC 那个 10:17 写下的盘中行就是这么自愈的(8/26 那行实测
      复算差 −0.00)。**这个前提若改成增量追加,这里必须跟着改**,否则会读到
      一行再没人碰过的陈值。
    """
    vals, dates = {}, {}
    cache: dict[str, list] = {}
    for st, (fn, field) in OFFICIAL_FIELDS.items():
        rows = cache.get(fn)
        if rows is None:
            rows = official_rows(DATA / fn)
            cache[fn] = rows
        if session is None:
            last = rows[-1]
        else:
            hits = [r for r in rows if str(r.get("date")) == session]
            if not hits:
                raise SourceError(
                    f"{fn} 里没有 {session} 那一行(末行 "
                    f"{rows[-1].get('date')})—— 那天的官方 EOD 还没发布"
                    f"(夜间 pipeline 未跑完 / 未跑齐)")
            if len(hits) > 1:
                # 同一天两行说明 merge 逻辑坏了。挑一行读下去就是在猜哪行是真的。
                raise SourceError(f"{fn} 里 {session} 有 {len(hits)} 行重复 —— "
                                  f"官方序列本身坏了,不猜哪行是真的")
            last = hits[0]
        if field not in last:
            raise SourceError(f"{fn} 的 {last.get('date')} 行缺字段 {field}")
        vals[st], dates[st] = float(last[field]), str(last["date"])
    if len(set(dates.values())) != 1:
        raise SourceError(f"五策略官方 EOD 日期不一致 {dates} —— 夜间 pipeline "
                          f"没跑齐就冻 K 会把一天的盈亏永久焊进常数里")
    return next(iter(dates.values())), vals


def qc_client_project():
    """→ (QcClient, project_id)。抽出来供对账平面复用:一次会话里 find_project
    只做一遍,免得每个只读检查各自再拉一次项目列表。"""
    from ops.deploy import ALGOS
    from qc_api import QcClient
    c = QcClient()
    pid = c.find_project(ALGOS["mirror"]["project"])
    if pid is None:
        raise SourceError("QC 上找不到 mirror 项目")
    return c, pid


def qc_snapshot(client=None, pid=None) -> dict:
    """QC 只读快照:逐票股数 + 持仓市值 + 现金 + 账户净值(自算与 QC 报的对拍)。

    client/pid 省略时自建(--measure/--freeze 的原有用法一字不变);对账平面
    传入已有会话,让持仓/订单/日志三次取数落在同一个 project 解析上。
    """
    if client is None or pid is None:
        client, pid = qc_client_project()
    c = client
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
    # equity = 同一份 payload 自算(持仓市值 + 现金)。**这不是收盘净值**:payload
    # 里的逐票价停在收盘前约 15 分钟,所以它是 15:45 那一刻的净值。留着只作审计
    # 留痕与陈旧度度量,K 与 M4 的 Q 都不许用它 —— 用官方收盘价逐票复算(official_q)。
    equity = mv + cash
    # gross = Σ|持仓市值|:对账把它当 bp 的分母(净额对市场中性簿是退化统计量,
    # 净额→0 时 bp→∞)。prices 按**仓库现行代码**归一化(QC 的 ORCC/NB/CMB
    # 是 security ID 的历史首名),这样残差表等本地文件可以直接按票取价。
    gross = sum(abs(float(v.get("v") or 0.0)) for v in holds.values())
    prices = {_canon(t): float(v.get("p") or 0.0)
              for t, v in holds.items() if v.get("p")}
    return {"shares": {t: s for t, s in shares.items() if s},
            "holdings": holds, "gross": gross, "prices": prices,
            "holdings_mv": mv, "cash": cash, "equity": equity,
            "equity_reported": eq_rep,
            # 曾叫 quiet_gap 并被当作"账户静不静"的判据 —— 它量的其实是 payload
            # 价格的陈旧度(收盘前 15 分钟到收盘之间那段行情),与静不静无关,
            # 收盘后也不会收敛到 0。改名是为了不让它再被当判据用。
            "price_staleness_usd": None if eq_rep is None else eq_rep - equity,
            "deploy_id": lr.get("deployId")}


def official_q(session: str, qc: dict) -> dict:
    """用官方收盘价把 QC 快照定值成该 session 的收盘净值。

    → {"Q", "closes", "cross_usd", "cross_bp"};取不到价一律抛 SourceError。
    与 M4 reconcile.qc_reconcile.equity_plane 是同一套定义,改一处必须改两处。
    """
    from reconcile import official_close
    shares = qc.get("shares")
    if not shares:
        raise SourceError("QC 快照里没有逐票股数 —— 定不出收盘 Q")
    closes = official_close.closes_for(session, [_canon(t) for t in shares])
    cash = float(qc["cash"])
    Q = cash + sum(int(s) * closes[_canon(t)] for t, s in shares.items())
    gross = float(qc.get("gross") or 0.0)
    rep = qc.get("equity_reported")
    cross = None if rep is None else Q - float(rep)
    return {"Q": Q, "closes": closes, "cross_usd": cross,
            "cross_bp": (None if cross is None or gross <= 0
                         else abs(cross) / gross * 1e4)}


def _et_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


# QC 一侧的符号命名怪癖:QC 按 security ID 记仓,展示名取该 ID 的**历史首名**,
# 所以 2023 年 ORCC→OBDC 改名后的仓在 live/portfolio 里仍叫 ORCC,而 target 用
# 现名 OBDC。这不是持仓不符,是两边叫法不同 —— 不归一化就会让退场日永远冻不了 K。
# 只放在这里(而不是仓库根 ticker_aliases.json):它描述的是 QC 怎么命名,不是市场
# 事件,写进那份全仓库共用的映射会波及 VP/价格归一化等无关链路。
QC_SYMBOL_ALIAS = {
    "ORCC": "OBDC",
    # 2026-08-24 F 队列首日实测:QC 给 BAC/JPM 的 security ID 历史首名分别是
    # NB(NationsBank,1998 并入 BankAmerica)与 CMB(Chemical Banking,
    # 经 Chase 并入 JPM)。股数与 target 分毫不差,纯命名差。
    "NB": "BAC",
    "CMB": "JPM",
    # 2026-08-31 MRPT ACGL/HIG 上车首日实测:ACGL(Arch Capital)的 ID 历史首名
    # 是 RCHI(Risk Capital Holdings,2000 年更名)。1248 股与 target 分毫不差、
    # QC 标价 98.20 vs 面板成本 98.84,纯命名差。不补的话 16:20 ① 误报 breach,
    # ③ 的 Q 会因 Polygon 查不到 RCHI 定不出收盘价。
    "RCHI": "ACGL",
}


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
    # Q 必须用**官方收盘价**逐票复算。取不到价就没有 Q,--measure 也不硬凑一个
    # 数出来看 —— 那个数会被当成"今天的 K"记在脑子里。
    oq = official_q(d, qc)
    Q, cross, cross_bp, closes = (oq["Q"], oq["cross_usd"], oq["cross_bp"],
                                  oq["closes"])
    K = P - Q
    print(f"\n官方 EOD {d}: " + " ".join(f"{s}={v:,.0f}" for s, v in off.items()))
    print(f"  面板/官方口径合计 P = {P:>16,.2f}")
    print(f"  QC 账户净值      Q = {Q:>16,.2f}"
          f"   (现金 {qc['cash']:,.2f} + Σ 股数×{d} 官方收盘价)")
    print(f"  K = P − Q         = {K:>16,.2f}")
    if cross is not None:
        ok = cross_bp is not None and cross_bp <= CROSS_TOL_BP
        print(f"  交叉校验: QC 自报 Equity {qc['equity_reported']:,.2f}"
              f" 与收盘价复算差 {cross:+,.2f}"
              f" ({'—' if cross_bp is None else f'{cross_bp:.2f}bp'},"
              f" {'一致 ✓' if ok else f'超 {CROSS_TOL_BP}bp — 股数/现金/收盘价有一样不对'})")
    stale = qc.get("price_staleness_usd")
    if stale is not None:
        print(f"  (参考)payload 自算净值 {qc['equity']:,.2f},比 QC 自报少"
              f" {stale:+,.2f} —— 这是逐票价停在收盘前 ~15 分钟造成的陈旧度,"
              f"不是账户在动;K 不用这个数)")
    if not freeze:
        prev = _load(ROLLOFF_PATH)
        if prev:
            drift = K - float(prev["k_equity"])
            print(f"\n已冻结 K = {float(prev['k_equity']):,.2f}"
                  f"(测于 {prev['measured_on']});今日实测偏离 {drift:+,.2f}")
            # 对死常数的偏离不是"漂移":每个换仓日的镜像滞后+滑点是永久台阶,
            # 会按日滚入 k_effective(M4 报告是台阶的唯一真相源)。
            # 真漂移看 D − K_eff,只应剩股息时点等会自己回冲的项。
            from reconcile.qc_reconcile import k_effective   # 函数内 import 免循环
            k_eff, n_steps, k_miss = k_effective(prev, d, include_upto=True)
            print(f"K_eff = {k_eff:,.2f}(冻结值 + {n_steps} 天台阶)"
                  f";D − K_eff = {K - k_eff:+,.2f}"
                  + (f";缺台阶 {k_miss}" if k_miss else ""))
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
    if cross is None:
        raise SourceError("拿不到 QC 自报净值 —— 收盘 Q 没有第二条路径可校验,"
                          "此刻冻 K 等于把一个没人复核过的数定成永久常数")
    if cross_bp is None or cross_bp > CROSS_TOL_BP:
        raise SourceError(f"收盘价复算的 Q({Q:,.2f})与 QC 自报净值"
                          f"({qc['equity_reported']:,.2f})差 {cross:+,.2f}"
                          f"({'gross 为 0' if cross_bp is None else f'{cross_bp:.2f}bp'}"
                          f" > {CROSS_TOL_BP}bp)—— 两条独立路径对不上,"
                          f"股数/现金/收盘价至少有一样是错的,不能冻")
    # M4 那份日报是对 Q 的独立复核(官方 EOD 日期一致、没有盘中写的 performance
    # 文件、交叉校验通过)。K 是永久常数,必须冻在**已经被对账平面判过**的那个
    # session 上,而不是"我这一趟自己算得挺顺"。
    m4 = _THIS_DIR / "reconcile" / f"qc_reconcile_{d}.json"
    doc4 = _load(m4)
    st4 = ((doc4 or {}).get("equity_check") or {}).get("status")
    if st4 not in ("ok", "baseline"):
        raise SourceError(
            f"M4 对账报告 {m4.name} 的 equity 段是 {st4 or '缺失'},不是 ok/baseline"
            f" —— 那趟对账没能给 {d} 的 Q 出裁决(官方 EOD 未落地 / 有盘中写的"
            f" performance 文件 / 交叉校验没过)。先把那天对平再冻 K")
    q4 = (doc4["equity_check"] or {}).get("qc_equity_Q")
    if q4 is None or abs(float(q4) - Q) > 1.0:
        raise SourceError(f"M4 判过的 Q({q4})与此刻复算的 Q({Q:,.2f})对不上"
                          f" —— 两趟之间账户变过(有成交/持仓变动),冻哪个都是错的")
    if d != et.strftime("%Y-%m-%d"):
        print(f"  注意: 官方 EOD 是 {d},不是今天({et:%Y-%m-%d} ET)"
              f" —— 确认这是最近一个已收盘交易日。")
    doc = {"frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "measured_on": d,
           "k_equity": round(K, 2),
           "panel_official_total": round(P, 2),
           "official_eod": {s: round(v, 2) for s, v in off.items()},
           "qc_equity": round(Q, 2),
           "q_basis": "cash + Σ 逐票股数 × 官方收盘价(Polygon 日 K)",
           "qc_equity_reported": round(float(qc["equity_reported"]), 2),
           "cross_check_usd": round(cross, 2),
           "cross_check_bp": round(cross_bp, 2),
           "qc_shares": dict(qc["shares"]),
           "official_closes": {t: closes[t] for t in sorted(closes)},
           "qc_equity_payload_selfcalc": round(qc["equity"], 2),
           "qc_holdings_mv": round(qc["holdings_mv"], 2),
           "qc_cash": round(qc["cash"], 2),
           "qc_deploy_id": qc["deploy_id"],
           "m4_report": m4.name,
           "n_tickers_matched": n_all,
           "note": "退场日实测:L/S 两队已清空且 QC 逐票收敛,两边持仓相同 ⇒ "
                   "净值差只剩现金项,定格为常数。恒等式是 "
                   "**按官方收盘价复算的 QC 净值** + k_equity ≡ 面板净值 —— "
                   "不是 QC 面板上显示的那个数,也不是 payload 自算的那个数"
                   "(后者停在收盘前 ~15 分钟,8/27 实测差 48.5bp)。"}
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
