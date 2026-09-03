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
  python ops/rolloff.py --freeze [--session YYYY-MM-DD]
                                     一次性: 报告锚定制冻结(K = 锚点场次 M4 报告
                                     已判的 D_usd;零 QC API,不要求账户静止)

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
# 2026-09-03 由 3.0 放宽到 5.0,与 qc_reconcile.CROSS_TOL_BP 同步(理由见那边注释:
# Q 是 16:00 收盘价口径、自报净值是 16:20 的实时读数,基差随规模放大)。
CROSS_TOL_BP = 5.0


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


def _mirrored_by(st: str, session: str | None, ex_state: dict) -> bool:
    """该策略在 session **收盘时**是否已被 QC 镜像(P 的入选判据)。

    scalar 在 = 挂载过;但挂载发生在某个时刻,之前的场次收盘时 QC 还没有这笔
    入金/持仓 —— 那些场次的 P 不能含它,否则 ΔD 凭空跳一整个策略净值。
    onboard_aeus 写 onboard_log[].at(UTC);判据 = at < session 收盘 16:00 ET。
    session=None(末行模式,--measure)或没有 at 记录(legacy/--force)→ 退回
    "scalar 在即算镜像"。
    """
    scalars = ex_state.get("scalars") or {}
    if st not in scalars:
        return False
    ats = [e.get("at") for e in ex_state.get("onboard_log") or []
           if e.get("strategy") == st and e.get("at")]
    if session is None or not ats:
        return True
    from zoneinfo import ZoneInfo
    close = datetime.fromisoformat(session).replace(
        hour=16, minute=0, tzinfo=ZoneInfo("America/New_York"))
    at = datetime.fromisoformat(max(ats).replace("Z", "+00:00"))
    return at < close


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
    # P 必须镜像"QC 正在镜像的那本书"。aeus 的官方序列先于 QC 挂载存在
    # (2026-08-31 实测 ~$1.16M 全历史已入 master json),若无条件计入,
    # 挂载前 P 凭空多一个策略、ΔD 跳 +$1.16M 假 breach;挂载(onboard_aeus
    # append scalar + CashBook 入金)当日起 P 与 Q 同步 +aeus,D 才连续。
    # 判据 = exporter scalars 里有没有该策略:scalar 是"QC 在镜像它"的唯一凭证。
    ex_state = _load(EXPORTER_STATE, {}) or {}
    scalars = ex_state.get("scalars") or {}
    for st, (fn, field) in OFFICIAL_FIELDS.items():
        if st == "aeus" and not _mirrored_by(st, session, ex_state):
            continue                      # 该场次收盘时 QC 尚未镜像 → 不入 P
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
    official_close.assert_prices_sane(closes, qc.get("prices"), _canon)
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
    # 2026-09-01 v18/v19 新空腿上车实测:TPR(Tapestry)的 ID 历史首名是
    # COH(Coach,2017 年更名)。−640 股与 target 分毫不差,同款纯命名差。
    "COH": "TPR",
    # 2026-09-02 AEUS 挂载首日实测,一次三例(股数与 target 逐位吻合、QC 标价 vs
    # 账本 avg_cost 同票):NEE 显示 FPL(Florida Power & Light,1990s 更名前身)、
    # GEV 显示 GEVW(GE Vernova 拆分时的 when-issued 代码)、LNT 显示 WPH
    # (WPL Holdings→Alliant)。新策略上车 = 必查历史首名,已成规律。
    "FPL": "NEE",
    "GEVW": "GEV",
    "WPH": "LNT",
    # 2026-09-03 RVTY(Revvity)上车:QC 显示 EG&G 的历史首名 EGG
    # (EG&G → PerkinElmer → Revvity)。**这一例和前八例性质不同,危险得多**:
    # RCHI/COH/FPL 那些在 Polygon 上根本不存在,查不到会抛错;而 EGG 在 Polygon
    # 上是**真实存在的另一家公司**(Enigmatig Limited,NYSE American,9/2 收 2.76,
    # 而 Revvity 收 130.94)。不加别名 → closes_for 查得到价、不报错、静默用错价,
    # 868 股会让 Q 少算 $111,260 = 129bp。故另加 assert_prices_sane 守卫(见
    # reconcile/official_close.py):把这类"映射到别的证券"从静默错价变成指名报错。
    "EGG": "RVTY",
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


def cmd_freeze_anchored(session: str | None = None) -> int:
    """报告锚定制冻结(2026-08-31,方案 A;plan 附录 A-4)。

    K = 锚点场次 M4 报告里**判过的** D_usd(= P(d) − Q(d),两个数都躺在报告里,
    已被三平面全套闸门复核:官方 EOD 按日期核对、盘中文件 mtime 闸、Q 与 QC 自报
    交叉校验、当日持仓逐票 0 差)。

    旧三闸("此刻实测 Q ±$1"/"17:00–04:00 窗口"/"此刻收敛")随 k_effective 一并
    退役:K 是**那个 session**的属性,不是"此刻"的。锚点之后每个换仓日的缺口恰是
    k_effective 要滚的台阶(镜像滞后+滑点),不需要账户静止 —— 每天有交易也随时
    可冻。本路径**零 QC API、零 Polygon**:全部输入来自盘上报告与队列状态。
    """
    if ROLLOFF_PATH.exists():
        raise SourceError(f"{ROLLOFF_PATH} 已存在 — K 只冻一次;确要重来先人工归档")
    q = alive_queues()
    if sum(len(v) for v in q.values()):
        raise SourceError("L/S 两队未清空 —— 未镜像腿的市值还在 D 里随行情漂,冻不得")
    rep_dir = _THIS_DIR / "reconcile"

    def _all_ok(doc: dict) -> bool:
        h = (doc.get("holdings_check") or {}).get("status")
        t = (doc.get("target_check") or {}).get("status")
        e = doc.get("equity_check") or {}
        return (h == "ok" and t == "ok" and e.get("status") in ("ok", "baseline")
                and e.get("D_usd") is not None)

    if session is None:
        # 默认锚点 = 最新一份三段全 ok 的报告。不悄悄跳过更近的坏报告不吭声 ——
        # 打印跳过了哪些,免得"我以为冻的是昨天"。
        for p in sorted(rep_dir.glob("qc_reconcile_*.json"), reverse=True):
            doc = _load(p)
            if doc and _all_ok(doc):
                session = p.stem.replace("qc_reconcile_", "")
                break
            print(f"  跳过 {p.name}(三段未全 ok)")
        if session is None:
            raise SourceError("没有任何一份三段全 ok 的 M4 报告可作锚点 —— 先等一个"
                              "干净 session 的裁决(--session 可显式指定)")
    path = rep_dir / f"qc_reconcile_{session}.json"
    doc = _load(path)
    if not doc:
        raise SourceError(f"锚点报告缺失: {path.name} —— 那天没有被对账平面判过,"
                          f"没有判过的数不配当永久常数")
    h = (doc.get("holdings_check") or {}).get("status")
    t = (doc.get("target_check") or {}).get("status")
    e = doc.get("equity_check") or {}
    if h != "ok":
        raise SourceError(f"锚点 {session} 的 holdings 段是 {h!r} 不是 ok —— "
                          f"那天持仓没有逐票配平,P−Q 里混着未镜像差")
    if t != "ok":
        raise SourceError(f"锚点 {session} 的 target 段是 {t!r} 不是 ok —— "
                          f"那天本地 target 与推送不一致,Q 对的书不可信")
    if e.get("status") not in ("ok", "baseline"):
        raise SourceError(f"锚点 {session} 的 equity 段是 {e.get('status') or '缺失'},"
                          f"不是 ok/baseline —— 那天的 D 没有拿到干净裁决,先把那天"
                          f"对平再冻(或 --session 换一天)")
    K = e.get("D_usd")
    if K is None:
        raise SourceError(f"锚点 {session} 的报告没有 D_usd —— 报告损坏或版本过旧")
    K = float(K)
    doc_out = {
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "measured_on": session,
        "k_equity": round(K, 2),
        "provenance": "m4_report_anchored",          # 方案 A:值取自已判报告,非实测
        "anchor_report": path.name,
        "panel_official_total": e.get("official_total_P"),
        "official_eod": e.get("official_eod"),
        "qc_equity": e.get("qc_equity_Q"),
        "q_basis": e.get("q_basis"),
        "cross_check_usd": e.get("cross_check_usd"),
        "cross_check_bp": e.get("cross_check_bp"),
        "note": "报告锚定制:K = 锚点场次已判的 P−Q。恒等式是 "
                "**Q + k_effective ≡ P**,k_effective = k_equity + Σ 锚点之后每个"
                "换仓日的(镜像滞后+滑点)台阶(从 M4 报告 attribution 累加,报告即"
                "台账);真漂移看 D − K_eff,只应剩股息时点等会自己回冲的项。"}
    _atomic_write(ROLLOFF_PATH, doc_out)
    print(f"[rolloff] 已冻结 K = {K:,.2f}(锚点 {session},取自 {path.name})"
          f" → {ROLLOFF_PATH}")
    return 0


def cmd_measure() -> int:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="legacy/S 退场日 K 常数实测与冻结")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="离线:还剩几对没退场")
    g.add_argument("--measure", action="store_true", help="只读:对拍 + 试算 K")
    g.add_argument("--freeze", action="store_true",
                   help="一次性:报告锚定制冻结 K(方案 A,plan 附录 A-4)")
    ap.add_argument("--session", default=None, metavar="YYYY-MM-DD",
                    help="冻结锚点场次(默认=最新一份三段全 ok 的报告)")
    a = ap.parse_args()
    try:
        if a.check:
            cmd_check()
            return 0
        if a.freeze:
            return cmd_freeze_anchored(a.session)
        return cmd_measure()
    except SourceError as e:
        print(f"!!!! [rolloff] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
