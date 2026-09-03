"""qc_reconcile — M4 对账平面:每交易日把 QC 云账户与本地口径对拍并留痕。

plan §Verification 平面三问,逐条落成三个独立 section(各自 status,互不掩盖):

  ① holdings_check  QC 逐票持股 vs exporter 已推送的 target —— 必须 0 差
  ② target_check    exporter 推的 target vs 当前五持仓文件现算的 target
                    —— 抓"exporter 死了/卡住,QC 在镜像一本陈书"
  ③ equity_check    §5 净值一致性预算:D = Σ官方EOD − QC净值,逐日增量分解

**这不是 controller/reconcile_eod.py 的重复**。那份(plan 编号 M5)只做本地
自洽:controller 账本收盘值 vs 用同一份 golden 持仓独立重算的值,**全程不碰
QC**。它 ok 只证明本地账本没算错,对"QC 云上那本书是否真的等于本地"一个字
都没说。M4 补的正是 QC↔本地这条腿。

────────────────────────────────────────────────────────────────────────────
为什么分两趟跑(16:20 与 23:15 ET),而不是塞进一个时点
────────────────────────────────────────────────────────────────────────────
三个 section 的**时点基准不同**,硬凑到一起必然产出假判定:

  ①②  需要"当前 QC 持仓"与"当前这本书"。16:20 ET 最干净:盘中变更已执行完,
      而夜间 pipeline(21:30 ET)还没把 inventory 换成明天的书。
  ③   需要**官方 EOD 净值**,它由 21:30 那趟 pipeline 生成 —— 16:20 时官方
      json 的末行还停在昨天。拿昨天的官方值去对今天的 QC 实时净值,量出来
      的是一整天的行情,不是对账误差。

所以:同一份报告、两趟写。每个 section 自带 status,没轮到的显式 `pending`
(带原因),**绝不用零或旧值冒充**。第二趟合并进同一个 session 文件。

派活口径是"还欠着哪天"(`unsettled_sessions`),不是"今天是哪天":官方 EOD 若
拖过次日 13:30(pipeline 晚起两三个钟头就会),16:20 一到 last_session 就换成
新一天,前一天再也轮不到,equity 段永久停在 pending。而 `prev_report` 会跳过它,
后一天的 ΔD 就悄悄变成跨两天的量 —— 滑点与"现金反解股息"两项却只吃当天成交,
混跨度算出来的残差是**错的**不是缺的。两条路都堵:派活按欠账清单走(§settle_all),
真漏了也有 equity_plane 的紧邻性闸门拦住不出裁决。
配套地,③ 取官方 EOD 一律**按 session 查行**而不是取末行(rolloff.official_eod):
取末行的话 P(D) 只在 "P(D) 落地 → P(D+1) 落地" 那不到一天里取得到。

①在 23:15 那趟会自动降级为 `pending_apply`:pipeline 换书后 exporter 会立刻
推新 target,而 QC 要等明早 09:30 才执行 —— 此刻 QC 持仓 ≠ 最新 target 是
**正确**行为。靠 QC 日志里的 `applied v{n} CONVERGED` 判断已应用版本,版本有
落差就不出 0 差裁决(而不是按点钟猜)。

────────────────────────────────────────────────────────────────────────────
③ 的分解恒等式(§5 + §9.2 三队列推广)
────────────────────────────────────────────────────────────────────────────
D(d) = P(d) − Q(d)      P = Σ 五策略官方 EOD 净值, Q = QC 账户净值

两边**口径已经对齐**(§9.2 缩放镜像定案):QC 从 C0=Σ官方equity 起步,
每票持股 = 账本股数 × 建仓常数 k_s,而官方口径:
    pairs(mrpt/mtfs) 加性族 official = ledger − C_s ⇒ 官方日变动 = 账本日变动,
                     F 队列 m=1 ⇒ QC 日变动 = 账本日变动 —— 逐日 1:1 ✓
    其余     乘性族 official = ledger × k_s,QC 持股 ×k_s —— 逐日 1:1 ✓
故稳态下 D 恒为常数(= 退场日要焊进 rolloff.json 的 K)。ΔD 才是被审的量。

过渡期 D 还会动,动的部分**可以逐项算出来**:
    ΔD ≈ + Σ_L  ΔPnL(pair)              L 队列 m=0:官方吃这份盈亏,QC 没有
       + Σ_S  (1−k_s)·ΔPnL(pair)        S 队列 m=k:QC 只吃了 k 份
       + Σ    滑点成本 (px_fill − px_ref)·qty
       + Δ    小数残差市值 Σ residual_t·px_t
       + Σ    股息时点项 (本地按除息日入账 vs QC 按付息日到现金)
       + ε    行情源差(QC NBBO vs 本地收盘)
剩下的叫 unattributed。判据(§5,按过渡期实情改写):
    无成交日   |ΔD − bootstrap 项| / gross > 5bp   → breach
    有成交日   |unattributed|      / gross > 3bp   → breach
(§5 原文把 5bp 挂在"drift 增量"上 —— 那是写在三队列方案之前的。过渡期
 L/S 的未镜像盈亏天然让 ΔD 大幅波动,不先扣掉它,阈值天天报警等于没有阈值。)

首次运行没有前一日报告 → 只记基准(status=baseline),不出净值裁决。

────────────────────────────────────────────────────────────────────────────
防火墙与写入面
────────────────────────────────────────────────────────────────────────────
只读:五持仓文件 / 五 account json / 三 public 绩效 json / state/ / QC 只读接口。
只写:reconcile/qc_reconcile_{session}.json。
**绝不下单,绝不写 inventory,绝不写 state/** —— 对账平面对执行平面零反向作用。

用法:
  python -m reconcile.qc_reconcile              自动:能算的都算,算不了的 pending
  python -m reconcile.qc_reconcile --session 2026-08-27   指定交易日(必须与
                                                 官方 EOD/账本 as_of 对得上)
  python -m reconcile.qc_reconcile --dry        只打印不写文件
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))

from inventory_source import (LEDGER_ACCOUNT_FILES, PAIR_STRATEGIES,  # noqa: E402
                              SourceError, stable_read)
from ops import rolloff                                              # noqa: E402
from reconcile import official_close                                 # noqa: E402

REPO = _PKG.parent
STATE_DIR = _PKG / "state"
REPORT_DIR = _PKG / "reconcile"
TARGET_COPY = STATE_DIR / "target_portfolio.json"
EXPORTER_STATE = STATE_DIR / "exporter_state.json"
RESIDUAL_PATH = STATE_DIR / "fractional_residual.json"
ROLLOFF_PATH = STATE_DIR / "rolloff.json"

# §5 阈值。分母一律 gross exposure(Σ|持仓市值|):定价/执行误差按被定价的
# 名义额缩放;净额对市场中性簿是退化统计量(净额→0 时 bp→∞)。
STEADY_TOL_BP = 5.0        # 无成交日:扣掉 bootstrap 项后的残差
REBAL_TOL_BP = 3.0         # 有成交日:全部可归因项扣完后的残差
# Q 的交叉校验:我们用官方收盘价逐票复算的净值,与 QC 引擎自报的
# runtimeStatistics.Equity 之差。两条路径互不相干,对得上才说明股数、现金、
# 收盘价三样都对。8/27 实测 −474.73 = 0.82bp。
#
# 原先这里是 QUIET_TOL(= rolloff 的 TOL_QUIET,±$500),比的是 QC 自报净值与
# **同一份 payload 自算值**之差。那两个数一新一陈,量到的是 payload 价格的陈旧
# 度(8/27 = 48.5bp),不是账户静不静,永远过不去。已废弃,不要复活。
# 2026-09-03 由 3.0 放宽到 5.0(用户决定)。这道闸门比的两个数**时点不同**:
# Q 用 16:00 官方收盘价,equity_reported 是 16:20 现场读的 runtimeStatistics.Equity
# (实时滚动值)。这段基差随账簿规模放大:8/31 0.46bp → 9/1 0.91bp → 9/2 3.01bp
# (AEUS 上车,票数 31→43、gross +16%、史上首次负现金),恰好把 3bp 顶穿,让一个
# **已被独立验证正确**的 Q 拿不到裁决 —— 9/2 实测:AEUS 账本净值与 Polygon 收盘价
# 重算逐分相同(−0.00),缺口在新老票之间分散,不是错价。
# 5bp 仍远小于这道闸门当初要抓的东西(payload 陈价 48.5bp),股数错、少一只票、
# 现金错这类结构性错误量级都远超 5bp,照样拦得住。
CROSS_TOL_BP = 5.0

LOG_WINDOW = 250        # live/logs/read 单次窗口硬上限(QC 端拒绝更大的请求)
MAX_LOG_WINDOWS = 8     # 最多回扫 2000 行;扫穿了宁可报错也不假装"未应用"

_TAG_VERSION = re.compile(r"\[MIRROR\] v(\d+)\b")
_APPLIED = re.compile(r"\[MIRROR\] applied v(\d+) CONVERGED")


# ── 基础工具 ────────────────────────────────────────────────────────────────

def _load(p: Path, default=None):
    return json.loads(p.read_text()) if p.exists() else default


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def report_path(session: str) -> Path:
    return REPORT_DIR / f"qc_reconcile_{session}.json"


def last_session(et: datetime) -> str:
    """此刻(ET)最近一个**已收盘**的 NYSE 交易日。

    用 pandas_market_calendars 的 market_close 逐日比对,不是"周末→周五"的
    近似 —— 半日市(13:00 收)与节假日都要精确,不然会把休市日当交易日、
    或在半日市当天 13:30 还判成"未收盘"。日历库缺失直接抛:对账时点基准
    是整份报告的地基,猜一个出来比不出报告危险得多。
    """
    try:
        import pandas_market_calendars as mcal
    except ImportError as e:
        raise SourceError(
            f"缺 pandas_market_calendars({e}) —— 无法确定'最近一个已收盘交易日';"
            f"对账两侧的时点基准全靠它,不做近似替代") from e
    cal = mcal.get_calendar("NYSE")
    sched = cal.schedule(start_date=str(et.date() - timedelta(days=14)),
                         end_date=str(et.date()))
    if sched.empty:
        raise SourceError("NYSE 日历取不到最近 14 天的排程")
    closes = sched["market_close"].dt.tz_convert(et.tzinfo)
    done = [d for d, c in zip(sched.index, closes) if c <= et]
    if not done:
        raise SourceError(f"{et:%Y-%m-%d %H:%M} ET 之前 14 天内没有已收盘交易日")
    return done[-1].strftime("%Y-%m-%d")


def close_window(session: str, et: datetime) -> tuple[datetime, datetime]:
    """session 的收盘时刻,与**下一个**交易日的开盘时刻。"""
    try:
        import pandas_market_calendars as mcal
    except ImportError as e:
        raise SourceError(f"缺 pandas_market_calendars({e}) —— 定不出收盘窗口") from e
    end = datetime.strptime(session, "%Y-%m-%d").date() + timedelta(days=10)
    sched = mcal.get_calendar("NYSE").schedule(start_date=session,
                                               end_date=str(end))
    if sched.empty or sched.index[0].strftime("%Y-%m-%d") != session:
        raise SourceError(f"{session} 不在 NYSE 排程里,定不出收盘/次开时刻")
    if len(sched) < 2:
        raise SourceError(f"{session} 之后 10 天内取不到下一个交易日开盘时刻")
    return (sched["market_close"].iloc[0].tz_convert(et.tzinfo),
            sched["market_open"].iloc[1].tz_convert(et.tzinfo))


def prev_trading_session(session: str) -> str:
    """session 的**前一个** NYSE 交易日。

    ΔD 的分母是"一天"。滑点与"现金反解股息"两项只吃 session 当天的成交,
    所以前一份报告必须恰好是紧邻的上一个交易日,不能是"最近一份出过 D 的"。
    这条要精确到节假日/半日市,同 last_session 一样不做周末近似。
    """
    try:
        import pandas_market_calendars as mcal
    except ImportError as e:
        raise SourceError(f"缺 pandas_market_calendars({e}) —— 定不出前一交易日") from e
    start = datetime.strptime(session, "%Y-%m-%d").date() - timedelta(days=14)
    sched = mcal.get_calendar("NYSE").schedule(start_date=str(start),
                                               end_date=session)
    days = [d.strftime("%Y-%m-%d") for d in sched.index]
    if not days or days[-1] != session:
        raise SourceError(f"{session} 不在 NYSE 排程里,定不出前一交易日")
    if len(days) < 2:
        raise SourceError(f"{session} 之前 14 天内没有交易日")
    return days[-2]


def unsettled_sessions(et: datetime, back_days: int = 14) -> list[str]:
    """近 back_days 天内 equity 段未出终态、且**存着收盘快照**的 session(升序)。

    存在的意义:wrapper 只按 last_session 派活,而 --settle 能补的窗口只有次日
    盘中那两趟。官方 EOD 若拖过 13:30(2026-08-27 那趟 pipeline 起步就晚了
    2h35m —— 23:05 才起,常态是 20:30),16:20 之后 last_session 就换成新一天,
    前一天再也不会被派到,那天的 equity 段就永久停在 pending。所以补算按
    "还欠着哪天"派活,不按"今天是哪天"。

    没有 close_snapshot 的一律不列:那种天补不回来(D 日收盘的逐票股数只在
    [16:00 D, 09:30 D+1) 可观测),列进来只会让闸门永远过不去。它造成的后果由
    equity_plane 的紧邻性闸门负责喊出来,不在这里假装还有救。
    """
    if not REPORT_DIR.exists():
        return []
    floor = str(et.date() - timedelta(days=back_days))
    out = []
    for p in REPORT_DIR.glob("qc_reconcile_*.json"):
        d = p.stem.replace("qc_reconcile_", "")
        if d < floor:
            continue
        try:
            doc = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if (doc.get("equity_check") or {}).get("status") in TERMINAL:
            continue
        if not doc.get("close_snapshot"):
            continue
        out.append(d)
    return sorted(out)


def in_close_window(session: str, et: datetime) -> tuple[bool, str]:
    """此刻读到的 QC 实时净值,是否还等于 session 的**收盘**净值。

    只在 [session 收盘, 下一交易日开盘) 之间成立。开盘一响,live/portfolio 报的
    就是新一天的盘中值 —— 拿它去减 P(session) 量出来的是隔夜跳空加一段盘中
    行情,而这个数会被写成基准 D 或 ΔD,外表完全正常、没有任何东西会报错。
    16:20/23:15/23:50 三个排期天然落在窗口内,只有手工跑才可能跑出去。
    """
    close, nxt_open = close_window(session, et)
    if et < close:
        return False, (f"此刻 {et:%m-%d %H:%M} 还没到 {session} 收盘"
                       f"({close:%H:%M})—— 读到的是盘中净值")
    if et >= nxt_open:
        return False, (f"此刻 {et:%m-%d %H:%M} 已越过下一交易日开盘"
                       f"({nxt_open:%m-%d %H:%M})—— QC 实时净值已是新一天的"
                       f"盘中值,不再等于 {session} 的收盘净值")
    return True, ""


TERMINAL = ("ok", "breach", "partial", "baseline")


def merge_section(prior: dict | None, fresh: dict) -> dict:
    """同一 session 的第二趟结果并入第一趟。

    两趟的强项不同(16:20 能出持仓 0 差,23:15 才有官方 EOD),后一趟直接覆盖
    会把前一趟的真裁决抹成 pending —— 面板与人只看最后一份文件,那等于"检查
    做过但看不见"。规则:终态优先;后一趟拿到终态就替换(它更新),前一趟是
    终态而后一趟不是就保留,并把后一趟的状态挂在 later_pass 里留痕。
    """
    if not prior:
        return fresh
    if fresh.get("status") in TERMINAL:
        if prior.get("status") in TERMINAL and prior["status"] != fresh["status"]:
            fresh = dict(fresh)
            fresh["earlier_pass"] = {"status": prior["status"],
                                     "note": prior.get("note")}
        return fresh
    if prior.get("status") in TERMINAL:
        out = dict(prior)
        out["later_pass"] = {"status": fresh.get("status"),
                             "note": fresh.get("note")}
        return out
    return fresh


def prev_report(session: str) -> dict | None:
    """session 之前、**净值段已出过基准**的最近一份报告(过渡期跨节假日安全)。"""
    if not REPORT_DIR.exists():
        return None
    cands = []
    for p in REPORT_DIR.glob("qc_reconcile_*.json"):
        d = p.stem.replace("qc_reconcile_", "")
        if d < session:
            cands.append((d, p))
    for _, p in sorted(cands, reverse=True):
        try:
            doc = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        eq = doc.get("equity_check") or {}
        if eq.get("D_usd") is not None:
            return doc
    return None


def onboard_step(session: str) -> float:
    """挂载台阶:该 session 内(上一场次收盘, 本场次收盘] 落地的策略挂载,其
    deposit_K 之和。

    QC 没有活的入金通道(算法 _init_cash 只在部署首版跑一次;改算法=重部署=
    重置 paper 账户),新策略挂载时 QC 用保证金建仓:Q 不变、P 从该场次起多出
    该策略官方净值 —— 差额恰是"QC 物理上没有的那笔钱",正是 K 的定义域。
    以 onboard_log[].deposit_K 作永久台阶滚入 k_effective,Q + K_eff ≡ P 照样
    成立;该场次内 官方净值(收盘) − deposit_K(挂载时刻) 的小差落 unattributed,
    一次性。时刻→场次的归属与 rolloff._mirrored_by 同一判据(at < 收盘 16:00 ET)。
    """
    st = _load(rolloff.EXPORTER_STATE, {}) or {}
    log = st.get("onboard_log") or []
    if not log:
        return 0.0
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    close = _dt.fromisoformat(session).replace(hour=16, minute=0, tzinfo=et)
    try:
        prev_close = _dt.fromisoformat(prev_trading_session(session)).replace(
            hour=16, minute=0, tzinfo=et)
    except SourceError:
        prev_close = None
    total = 0.0
    for e in log:
        at_s, dep = e.get("at"), e.get("deposit_K")
        if not at_s or dep is None:
            continue
        at = _dt.fromisoformat(str(at_s).replace("Z", "+00:00"))
        if at < close and (prev_close is None or at >= prev_close):
            total += float(dep)
    return total


def k_effective(frozen: dict, upto_session: str, include_upto: bool = False
                ) -> tuple[float, int, list[str]]:
    """冻结后的有效常数:k_equity + Σ 每个换仓日的永久台阶(镜像滞后+滑点)。

    K 冻在 measured_on 那天的 D 上,但 D 不是从此不动:每个换仓日,面板按
    决策收盘入账、QC 次日成交才接上,这段差**永久**沉淀进 D(见
    rebalance_mirror_lag 项)。恒等式因此升级为
        Q + k_effective ≡ P,  k_effective = k_equity + Σ steps
    台阶不另设台账,直接从历史报告的 attribution 里读 —— 报告本来就是审计
    留痕,再写一份就是第二个真相源。返回 (k_eff, 已计台阶数, 缺台阶的
    session 列表);缺的那些(报告缺失或还停在 pending)让调用方**明说**,
    静默跳过会把 k_eff 的洞演成"漂移"。

    equity_plane 传 include_upto=False:当天自己的台阶调用方手上就有
    (lag + slip),从内存加,不依赖自己那份还没写盘的报告。
    rolloff --measure 传 True:纯读盘,当天的报告(若已出)也计入。
    """
    k = float(frozen["k_equity"])
    f_sess = str(frozen["measured_on"])
    n_steps, missing = 0, []
    if not REPORT_DIR.exists():
        return k, 0, []
    for p in sorted(REPORT_DIR.glob("qc_reconcile_*.json")):
        s = p.stem.replace("qc_reconcile_", "")
        in_range = (f_sess < s <= upto_session) if include_upto else \
                   (f_sess < s < upto_session)
        if not in_range:
            continue
        a = (((_load(p) or {}).get("equity_check") or {})
             .get("attribution") or {})
        lag = (a.get("rebalance_mirror_lag") or {}).get("usd")
        sl = (a.get("slippage") or {}).get("usd")
        ob_s = onboard_step(s)         # 台阶的真源是 exporter_state,不是报告
        if lag is None and sl is None:
            missing.append(s)          # 那天还没出归因(pending/缺文件)
            k += ob_s                  # 归因缺席不该顺手吞掉挂载台阶
            continue
        k += (lag or 0.0) + (sl or 0.0) + ob_s
        n_steps += 1
    return k, n_steps, missing


# ── QC 只读取数 ─────────────────────────────────────────────────────────────

def _of_deploy(orders: list[dict], deploy_id: str) -> list[dict]:
    """只留当前部署的订单记录。

    归属看 events 里的 algorithmId(order 记录本身不带部署标识)。一张单的所有
    event 同属一个部署,所以取第一个非空 algorithmId 即可判归属;整条记录归属
    别的部署就整条丢弃,不做逐 event 拆分 —— 混部署的单不存在"一半是我们的"。
    没有任何 event 的记录(已提交未成交)无法判归属:它对 fills 没贡献,丢掉是
    安全的,留下反而会让"当日有单在飞"的判断被死部署的陈单污染。
    """
    keep = []
    for o in orders:
        aid = next((e.get("algorithmId") for e in (o.get("events") or [])
                    if e.get("algorithmId")), None)
        if aid == deploy_id:
            keep.append(o)
    return keep


def qc_orders(c, pid, deploy_id: str, page: int = 100,
              hard_cap: int = 5000) -> list[dict]:
    """当前部署的全部历史订单(分页 + 按 deployId 过滤)。

    live/orders/read 首次调用会返回 {"status":"loading","progress":0.0} —— QC
    在后台准备结果,要轮询到 payload 里出现 orders 键。实测(2026-08-27)第一
    次 loading、第二次即就绪。不轮询就会静默拿到空单列表 → 把换仓日误判成
    无成交日 → 用错阈值(5bp 而非 3bp)。

    **必须按 deployId 过滤**(2026-08-28 实测):这个端点返回的是**项目**下所有
    历史部署的订单,不是当前部署的。本项目重部署过三次,111 条记录里只有 67 个
    不同 order id —— id 1..N 在每个部署里各出现一次(events 的 algorithmId 不同,
    order id 与 event id 都各自从头编号,单看 id 无法去重)。
    只按当前部署的 fills 重算持仓,与 live/portfolio 逐票严丝合缝;混着算则每票
    都是三倍。今天不咬人只因为死部署的单都停在 8/17–8/18;下一次重部署那天,
    死部署当天的单会和活部署的单同日出现,fills_of_session 会把换手额翻倍,
    滑点归因和 3bp/5bp 阈值一起错 —— 而那恰恰是最需要对账的一天。
    """
    if not deploy_id:
        raise SourceError("qc_orders 缺 deploy_id —— 不按部署过滤会把历史部署的"
                          "订单混进当日成交流水")
    out: list[dict] = []
    start = 0
    while start < hard_cap:
        d = None
        for attempt in range(10):
            d = c.call("live/orders/read", {"projectId": pid,
                                            "start": start, "end": start + page})
            if "orders" in d:
                break
            time.sleep(3)
        if d is None or "orders" not in d:
            raise SourceError(f"live/orders/read 轮询 10 次仍是 "
                              f"{d.get('status') if d else 'no-response'} —— "
                              f"拿不到成交流水就不出裁决")
        batch = d.get("orders") or []
        out.extend(batch)
        total = d.get("length")
        start += page
        if not batch or (total is not None and start >= int(total)):
            return _of_deploy(out, deploy_id)
    # 走到这儿说明订单数超过 hard_cap。绝不能静默截断:少读到的若正好是当日
    # 成交,换仓日会被当成无成交日 → 用 5bp 而不是 3bp,还会漏掉滑点项。
    raise SourceError(f"历史订单超过 hard_cap={hard_cap} 条仍未读完 —— "
                      f"截断的订单流水会让当日成交判定失真,不出裁决")


def qc_applied_version(c, pid) -> int | None:
    """QC 端**已收敛应用**的 target 版本(算法日志里的权威记录)。

    为什么不读 ObjectStore 的 mirror/last_applied.json:object/get 被 QC 以
    "数据授权限制,仅机构账户可导出"拒绝(2026-08-27 实测),这条路对本账户
    永久不通。也不用"订单 tag 里的最大版本号"——某版若无需下单就直接收敛,
    压根不会产生带 tag 的订单,那样会把已应用的版本误判成落后。

    翻页:live/logs/read 单次窗口硬上限 250 行(超了直接报错,2026-08-27 实测),
    start/end 是**绝对行号**,length 是日志总行数。所以从尾部往回一窗一窗扫,
    命中即停——健康状态下最新的 applied 就在最后一窗里,只花一次请求。
    """
    head = c.live_logs(pid, 0, 1)
    n = int(head.get("length") or 0)
    if not n:
        return None
    end = n
    for _ in range(MAX_LOG_WINDOWS):
        start = max(0, end - LOG_WINDOW)
        d = c.live_logs(pid, start, end)
        best = None
        for line in (d.get("logs") or []):
            m = _APPLIED.search(line if isinstance(line, str) else str(line))
            if m:
                v = int(m.group(1))
                best = v if best is None else max(best, v)
        if best is not None:
            return best          # 版本号单调递增,最靠后那一窗的最大值即最新
        if start == 0:
            return None          # 整段日志都没有 —— 算法自部署以来从未收敛
        end = start
    raise SourceError(f"回扫 {MAX_LOG_WINDOWS * LOG_WINDOW} 行日志仍没找到 "
                      f"'applied vN CONVERGED' —— 判不出 QC 应用到哪一版就不出裁决")


def fills_of_session(orders: list[dict], session: str, et_tz) -> list[dict]:
    """该交易日 ET 内的逐笔成交(order events 展开;一单多次成交各算一笔)。"""
    out = []
    for o in orders:
        sub = o.get("orderSubmissionData") or {}
        tag = o.get("tag") or ""
        m = _TAG_VERSION.search(tag)
        attrib = {}
        j = tag.find("{")
        if j >= 0:
            try:
                attrib = json.loads(tag[j:])
            except json.JSONDecodeError:
                attrib = {}
        for ev in (o.get("events") or []):
            if str(ev.get("status", "")).lower() not in ("filled",
                                                         "partiallyfilled"):
                continue
            q = float(ev.get("fillQuantity") or 0.0)
            if q == 0.0:
                continue
            ts = ev.get("time")
            if ts is None:
                continue
            when = datetime.fromtimestamp(float(ts), tz=timezone.utc
                                          ).astimezone(et_tz)
            if when.strftime("%Y-%m-%d") != session:
                continue
            out.append({
                "ticker": (o.get("symbol") or {}).get("value"),
                "order_id": o.get("id"),
                "qty": q,
                "fill_px": float(ev.get("fillPrice") or 0.0),
                "at_et": when.isoformat(timespec="seconds"),
                "target_version": int(m.group(1)) if m else None,
                "attribution": attrib,
                "submit_last_px": sub.get("lastPrice"),
                "submit_bid": sub.get("bidPrice"),
                "submit_ask": sub.get("askPrice"),
                "fee": float(ev.get("orderFeeAmount") or 0.0),
            })
    return sorted(out, key=lambda r: r["at_et"])


# ── ① 持仓 0 差 ─────────────────────────────────────────────────────────────

def holdings_plane(qc: dict, applied_version: int | None) -> dict:
    tgt = _load(TARGET_COPY)
    if not tgt:
        return {"status": "incomplete", "note": f"缺 {TARGET_COPY.name}"}
    pushed = int(tgt.get("version") or 0)
    row = {"pushed_version": pushed, "applied_version": applied_version,
           "content_hash": tgt.get("content_hash"),
           "exported_at": tgt.get("exported_at")}
    if applied_version is not None and applied_version < pushed:
        # 已推未执行(夜间换书后的常态:QC 要等下一个 09:30)。此刻拿最新
        # target 去要求 0 差是拿明天的答案考今天,不出裁决而不是判 breach。
        row.update(status="pending_apply",
                   note=f"exporter 已推 v{pushed},QC 最后收敛在 v{applied_version}"
                        f" —— 新目标待下一个开盘执行,本趟不出 0 差裁决")
        return row
    if applied_version is not None and applied_version > pushed:
        # QC 应用的版本比本地文件还新 = state/target_portfolio.json 被回滚/覆盖过。
        # 此时本地这份根本不是 QC 正在镜像的那本书,拿它比对毫无意义。
        row.update(status="breach",
                   note=f"QC 已应用 v{applied_version},本地 target 文件却只有 "
                        f"v{pushed} —— state/target_portfolio.json 被回滚或覆盖,"
                        f"本地已不知道 QC 在镜像什么。先查 exporter 状态与该文件")
        return row
    bad, n_all = rolloff.convergence(qc["shares"])
    row.update(n_tickers=n_all, n_matched=n_all - len(bad),
               tolerance_shares=rolloff.TOL_SHARES,
               diffs=[{"ticker": t, "qc": a, "target": b, "diff": a - b}
                      for t, a, b in bad],
               status="ok" if not bad else "breach")
    if bad:
        row["note"] = (f"{len(bad)} 票未收敛到 v{pushed} —— 逐票差见 diffs"
                       f"(容忍度 0 股:整数股镜像差 1 股就是没收敛或有单在飞)")
    return row


# ── ② target 新鲜度(exporter 是否还活着且推的是当前这本书)────────────────

def target_plane(composed: dict) -> dict:
    """现算 target 与已推 target 对拍。

    不用"文件 mtime / exported_at 有多老"当判据:exporter 只在内容变化时才
    重写 target_portfolio.json(exporter.py export_once),不变就不写 —— 一份
    六小时前的文件可能完全正确,也可能是 exporter 六小时前就死了。唯一能区分
    两者的判据是**拿当前持仓文件重算一遍**。
    """
    c = composed
    tgt = _load(TARGET_COPY) or {}
    pushed = tgt.get("targets") or {}
    rebuilt = c["built"]["targets"]
    names = sorted(set(pushed) | set(rebuilt))
    diffs = [{"ticker": t, "pushed": int(pushed.get(t, 0)),
              "rebuilt": int(rebuilt.get(t, 0)),
              "diff": int(rebuilt.get(t, 0)) - int(pushed.get(t, 0))}
             for t in names if int(pushed.get(t, 0)) != int(rebuilt.get(t, 0))]
    row = {"pushed_hash": tgt.get("content_hash"), "rebuilt_hash": c["hash"],
           "pushed_version": tgt.get("version"),
           "exported_at": tgt.get("exported_at"),
           "n_diff": len(diffs), "diffs": diffs[:40],
           "status": "ok" if not diffs else "breach"}
    if diffs:
        row["note"] = ("现算 target 与已推 target 不一致 —— exporter 没跟上"
                       "(死了/卡住/source error 后 fail-static),QC 正在镜像"
                       "一本陈书。查 com.someopark.qcmirror.exporter 与其日志")
    return row


# ── ③ 净值一致性预算 ────────────────────────────────────────────────────────

def _queue_pnl(session: str, prev_session: str, built: dict, snap: dict,
               scalars: dict) -> dict:
    """L/S 两队"未被镜像"的当日盈亏(§9.2 过渡项)。

    数据源 = inventory 里每对的 monitor_log.unrealized_pnl 逐日序列(账本美元)。
    pairs 是加性族(官方日变动 = 账本日变动),故 L 队(m=0)整份进项,
    S 队(m=k)只有 (1−k) 那部分没被镜像。

    取不到某对的两日读数就**逐对显式列进 unresolved**,绝不当 0 —— 一对
    几万美元的漏项会把整个残差判定变成噪声。
    """
    out = {"legacy_usd": 0.0, "scaled_usd": 0.0, "per_pair": [],
           "unresolved": [], "prev_session": prev_session}
    for st in PAIR_STRATEGIES:
        alive = {"L": (built["legacy_alive"] or {}).get(st, []),
                 "S": (built["scaled_alive"] or {}).get(st, [])}
        if not (alive["L"] or alive["S"]):
            continue
        # 用 reconcile 那一次的同一份 snap:重读一遍持仓文件会与 built 的存活
        # 判定错位(盘中平仓恰好落在两次读之间 → 报表内部自相矛盾)。
        inv = snap[st]
        k = float((scalars or {}).get(st, 1.0))
        for queue, items in alive.items():
            for it in items:
                rec = (inv.get("pairs") or {}).get(it["pair"]) or {}
                log = {r.get("date"): r.get("unrealized_pnl")
                       for r in (rec.get("monitor_log") or [])}
                a, b = log.get(prev_session), log.get(session)
                if a is None or b is None:
                    out["unresolved"].append(
                        {"strategy": st, "queue": queue, "pair": it["pair"],
                         "have_prev": a is not None, "have_session": b is not None,
                         "why": "monitor_log 缺该日 unrealized_pnl"})
                    continue
                d = float(b) - float(a)
                share = d if queue == "L" else d * (1.0 - k)
                out["per_pair"].append(
                    {"strategy": st, "queue": queue, "pair": it["pair"],
                     "pnl_prev": float(a), "pnl_session": float(b),
                     "delta_usd": round(d, 2),
                     "unmirrored_factor": 1.0 if queue == "L" else round(1 - k, 6),
                     "unmirrored_usd": round(share, 2)})
                if queue == "L":
                    out["legacy_usd"] += share
                else:
                    out["scaled_usd"] += share
    out["legacy_usd"] = round(out["legacy_usd"], 2)
    out["scaled_usd"] = round(out["scaled_usd"], 2)
    out["total_usd"] = round(out["legacy_usd"] + out["scaled_usd"], 2)
    return out


def intraday_official_files(session: str) -> list[str]:
    """官方 EOD 文件里,末行日期 = session 却在该日收盘**之前**就写完没再动的。

    2026-08-27 实测踩到:BDC 的 perf json 末行是 08-27,但文件 10:17 就写完了
    (当日 16:00 才收盘),那个 bdc_equity 是盘中值。等其余四个策略夜里落到
    08-27,五策略日期一致性闸门会直接放行 —— 日期对、值是陈的,那道闸门按定义
    抓不到。而当晚恰好是 baseline 日,错的 P 会被焊成基准 D,次日 ΔD 平白多出
    "盘中→收盘"那段移动,大概率误判 breach。

    判据只用来**拒绝**、绝不用来放行:mtime 会被 touch/复制重置,所以它单向可信
    —— 说"这文件在收盘前就定稿了"是硬证据,说"在收盘后写的"不等于内容就对。

    但光看 mtime 太钝,会误杀:2026-08-27 的 BDC 就是——perf json 10:17 写的没错,
    可另有一份 16:07(收盘后)独立写出的 daily_report 给出**同一个** bdc_equity,
    值本身是收盘值,只是 Step D 见"今天已有行"就幂等跳过、没去刷新文件。这种情形
    拦下来就是白白丢一天对账。所以补一层**值级佐证**:若存在收盘后写出的独立产物
    且其值与 perf json 末行一致,则视为已证实,放行。
    """
    import pandas_market_calendars as mcal
    et = rolloff._et_now().tzinfo
    sched = mcal.get_calendar("NYSE").schedule(start_date=session,
                                               end_date=session)
    if sched.empty:
        raise SourceError(f"{session} 不在 NYSE 排程里,判不了收盘时点")
    close = sched["market_close"].iloc[0].tz_convert(et)
    out = []
    for fn in sorted({f for f, _ in rolloff.OFFICIAL_FIELDS.values()}):
        p = rolloff.DATA / fn
        if not p.exists():
            continue
        # mtime 判据只在 session 还是**末行**时说得通:它量的是"整份文件最后
        # 一次落盘的时刻",能证明的只有末行那天的成色。文件里 session 之后还有
        # 行,就说明至少有一趟 session 之后的收盘后跑批把整段重算重写过
        # (三份文件都是全历史重写,见 rolloff.official_eod 的说明),session
        # 那行不可能还停在盘中值 —— 此时再用整份文件的 mtime 去判它,判的是
        # 别人家的时刻,只会把补得回来的天白白拦掉。
        try:
            rows = rolloff.official_rows(p)
        except SourceError:
            # 读不出内容(空/半截)就退回纯 mtime 判据 —— 那是偏严的一边。
            # 走到这里时 official_eod 其实已经把同样的文件读通过一遍了。
            rows = []
        if rows and str(rows[-1].get("date") or "") > session:
            continue
        mt = datetime.fromtimestamp(p.stat().st_mtime, tz=et)
        if mt >= close:
            continue
        ok, why = _corroborated(fn, session, close, et)
        if ok:
            continue
        out.append(f"{fn}(写于 {mt:%m-%d %H:%M},早于 {close:%m-%d %H:%M} 收盘;"
                   f"{why})")
    return out


# 收盘后独立产物,用来给"盘中定稿"的官方 EOD 文件做值级佐证。
# 路径含 {session};取值路径是嵌套 key 序列;只覆盖列出的字段。
CORROBORATORS = {
    "private_credit_bdc_performance.json": {
        "path": "portfolio_of_private_credit_deals/bdc_results/"
                "daily_report_{session}.json",
        "value_at": ("stock_layer", "bdc_equity"),
        "field": "bdc_equity",
    },
}


def _corroborated(fn: str, session: str, close, et) -> tuple[bool, str]:
    """该 perf 文件末行的值,是否被一份收盘后写出的独立产物证实。"""
    spec = CORROBORATORS.get(fn)
    if not spec:
        return False, "无收盘后独立产物可佐证"
    p = REPO / spec["path"].format(session=session)
    if not p.exists():
        return False, f"佐证文件不存在 {p.name}"
    mt = datetime.fromtimestamp(p.stat().st_mtime, tz=et)
    if mt < close:
        return False, f"佐证文件 {p.name} 也是收盘前({mt:%H:%M})写的"
    doc = _load(p) or {}
    for k in spec["value_at"]:
        doc = (doc or {}).get(k) if isinstance(doc, dict) else None
    rows = _load(rolloff.DATA / fn) or []
    if not rows or doc is None:
        return False, f"佐证值或 perf 末行取不到"
    got = rows[-1].get(spec["field"])
    if str(rows[-1].get("date")) != session:
        return False, f"perf 末行日期 {rows[-1].get('date')} ≠ {session}"
    if got is None or abs(float(got) - float(doc)) > 0.01:
        return False, (f"佐证值 {doc} 与 perf 末行 {got} 不符 —— "
                       f"perf 里那行确实是陈的")
    return True, ""


def _ledger_accounts(session: str) -> tuple[dict, list[str]]:
    """五本 account json(须 as_of == session,否则该策略列进 stale)。"""
    acc, stale = {}, []
    for st, rel in LEDGER_ACCOUNT_FILES.items():
        if st == "aeus" and not (REPO / rel).exists():
            continue        # go-live(9/1)前预期缺席,不入对账也不报 stale
        d = stable_read(REPO / rel)
        if str(d.get("as_of")) != session:
            stale.append(f"{st}:as_of={d.get('as_of')}")
        acc[st] = d
    return acc, stale


def equity_plane(session: str, qc: dict, fills: list[dict], built: dict | None,
                 snap: dict | None, scalars: dict,
                 residual: dict | None = None) -> dict:
    row: dict = {"session": session}
    if built is None or snap is None:
        return {"status": "pending", "session": session,
                "note": "持仓文件快照读不出来(见 target_check)—— 过渡期未镜像"
                        "盈亏算不了,不出净值裁决"}
    # 官方 EOD:**按 session 查行**,不取末行。取末行的话,P(D) 只在
    # "P(D) 落地 → P(D+1) 落地" 那不到一天里能取到,过了就永久补不了 D 了。
    # 五策略必须同一天、且必须就是 session —— 两条都由 official_eod(session)
    # 保证:哪份文件缺这一行就抛,所以这里拿到的 d_off 必然等于 session,
    # 不再需要单独一道日期比对(那道闸门是"取末行"时代的产物)。
    try:
        d_off, off = rolloff.official_eod(session)
    except SourceError as e:
        return {"status": "pending", "session": session,
                "note": f"{session} 的官方 EOD 取不到: {e} —— 拿别的日子的官方"
                        f"净值去对 {session} 收盘的 QC 净值,量出来的是一整天"
                        f"行情不是对账误差,故不出裁决"}
    stale = intraday_official_files(session)
    if stale:
        return {"status": "pending", "session": session, "official_date": d_off,
                "intraday_official": stale,
                "note": f"官方 EOD 有文件是**盘中**写的、事后没再更新: {stale} —— "
                        f"日期是 {session} 但值不是收盘值。日期一致性闸门抓不到这种"
                        f"(日期对、值是陈的),放过去会把错的 P 焊进基准 D,"
                        f"明天的 ΔD 就凭空多出这段盘中到收盘的移动"}
    # ── Q:逐票股数 × 官方收盘价 + 现金 ───────────────────────────────────────
    # 不用 QC payload 里的逐票价。那份价停在收盘前约 15 分钟(2026-08-28 实测,
    # 详见 reconcile/official_close.py 顶部),8/27 差 48.5bp —— 静止阈值的十倍。
    shares = qc.get("shares")
    if shares is None:
        return {"status": "pending", "session": session,
                "note": "QC 快照里没有逐票股数(旧版收盘存档)—— 没有股数就无法用"
                        "官方收盘价定 Q,而 payload 自算的净值是盘中值,不出裁决"}
    try:
        closes = official_close.closes_for(
            session, [rolloff._canon(t) for t in shares])
        # 错映射守卫:官方价与 QC 自己的价差一个数量级 = 映射到别的证券了
        # (EGG=Revvity 而 Polygon 的 EGG 是 Enigmatig)。不拦的话 Q 静默错 129bp,
        # 表现成"交叉校验失败",查不到根因。
        official_close.assert_prices_sane(closes, qc.get("prices"),
                                          rolloff._canon)
    except SourceError as e:
        return {"status": "pending", "session": session,
                "note": f"官方收盘价不可用: {e}"}
    cash = float(qc["cash"])
    Q = cash + sum(int(s) * closes[rolloff._canon(t)]
                   for t, s in shares.items())

    # QC 自报净值降级为**独立交叉校验**:同一个收盘、两条互不相干的路径
    # (QC 引擎自己的 TotalPortfolioValue vs 我们用 Polygon 收盘价逐票复算)。
    # 对不上说明股数、现金或收盘价至少有一样是错的 —— 那时不出裁决,而不是
    # 挑一个信。这才是原先 quiet_gap 想干却干不了的事:它比的是同一份 payload
    # 的两个字段,一陈一新,量到的是价格陈旧度,不是账户静不静。
    gross = float(qc["gross"])
    q_rep = qc.get("equity_reported")
    cross = None if q_rep is None else Q - float(q_rep)
    if cross is not None and gross > 0:
        cross_bp = abs(cross) / gross * 1e4
        if cross_bp > CROSS_TOL_BP:
            return {"status": "pending", "session": session,
                    "qc_equity_Q": round(Q, 2),
                    "qc_equity_reported": round(float(q_rep), 2),
                    "cross_check_usd": round(cross, 2),
                    "cross_check_bp": round(cross_bp, 2),
                    "note": f"官方收盘价复算的 Q({Q:,.2f})与 QC 自报净值"
                            f"({float(q_rep):,.2f})差 {cross:+,.2f}"
                            f"({cross_bp:.2f}bp > {CROSS_TOL_BP}bp)—— 两条独立"
                            f"路径对不上,股数/现金/收盘价至少有一样是错的,"
                            f"不出裁决"}
    P = sum(off.values())
    D = P - Q
    row.update(official_date=d_off,
               official_eod={s: round(v, 2) for s, v in off.items()},
               official_total_P=round(P, 2), qc_equity_Q=round(Q, 2),
               q_basis="cash + Σ 逐票股数 × 官方收盘价(Polygon 日 K)",
               qc_equity_reported=(None if q_rep is None
                                   else round(float(q_rep), 2)),
               cross_check_usd=(None if cross is None else round(cross, 2)),
               cross_check_bp=(None if cross is None or gross <= 0 else
                               round(abs(cross) / gross * 1e4, 2)),
               qc_holdings_mv=round(Q - cash, 2),
               qc_cash=round(cash, 2), gross_exposure=round(gross, 2),
               D_usd=round(D, 2))
    frozen = _load(ROLLOFF_PATH)
    if frozen:
        row["k_frozen"] = float(frozen["k_equity"])
        row["D_minus_K_usd"] = round(D - float(frozen["k_equity"]), 2)

    # 小数残差市值:官方口径持有 x 股,QC 只持 round(x) 股,差额按**官方收盘价**
    # 定值 —— 与 Q 同一套价,否则残差项和 Q 各按各的价算,差额会直接漏进
    # unattributed。QC 当天没持有的票不在 closes 里 → 逐票列进 unpriced,不硬凑。
    # residual 由调用方传入时用传入的:--settle 那趟跑在次日,state/ 里的残差
    # 可能已被当天的新一轮推送覆盖,必须用 D 日收盘存下的那份。
    if residual is None:
        residual = _load(RESIDUAL_PATH) or {}
    res = residual.get("residual") or {}
    frac_usd, unpriced = 0.0, []
    for t, r in res.items():
        p = closes.get(rolloff._canon(t))
        if p is None:
            unpriced.append(t)
            continue
        frac_usd += float(r) * float(p)
    row["fractional_residual"] = {"value_usd": round(frac_usd, 2),
                                  "n_tickers": len(res),
                                  "unpriced": sorted(unpriced)}

    acc, stale = _ledger_accounts(session)
    row["ledger_equity"] = {st: round(float(a.get("equity") or 0.0), 2)
                            for st, a in acc.items()}
    if stale:
        row["ledger_stale"] = stale

    prev = prev_report(session)
    if prev is None:
        row["status"] = "baseline"
        row["note"] = ("首份报告:没有前一日 D 可比,只落基准。"
                       "下一交易日起出 ΔD 分解与裁决")
        return row
    pe = prev["equity_check"]
    d_prev = pe["session"]

    # 前一份必须恰好是**紧邻的**上一个交易日。prev_report 会跳过没出 D 的报告,
    # 中间只要漏掉一天,ΔD 就变成跨两天的量,而下面的项**不是同一个跨度**:
    #   _queue_pnl 按 (prev_session, session) 两点取值,跨两天是对的;
    #   滑点只累 session 当天的 fills —— 漏掉那天的滑点;
    #   股息项由现金恒等式反解 qc_div = Δcash + fill_cash + fees,Δcash 跨了两天
    #     而 fill_cash 只有一天 ⇒ 漏掉那天成交的**整笔名义现金流**会冒充成股息。
    # 混跨度算出来的 unattributed 不是"漏了一项"而是"错了一项",给 partial 都算
    # 抬举它。所以不出裁决,先把中间那天补上(--settle --session <那天>)。
    # D_usd 照常留在报告里:它只依赖 P 与 Q,上面每道闸门都过了,是个可信的锚点
    # ——留着,明天的 prev_report 才能接上它,链条不至于一断就再也接不回来。
    try:
        expect = prev_trading_session(session)
    except SourceError as e:
        row["status"] = "pending"
        row["note"] = f"定不出 {session} 的前一交易日: {e} —— ΔD 跨度不明,不出裁决"
        return row
    if d_prev != expect:
        row["status"] = "pending"
        row["prev"] = {"session": d_prev, "D_usd": pe["D_usd"]}
        row["expected_prev_session"] = expect
        row["note"] = (
            f"最近一份出过 D 的报告是 {d_prev},但 {session} 的前一交易日是 "
            f"{expect} —— 中间的 {expect} 还没出 D(equity 段停在 pending 或报告"
            f"缺失)。ΔD 会变成跨多日的量,而滑点/股息两项只吃当天成交,混跨度"
            f"算出来的残差是错的不是缺的。先补 {expect}"
            f"(python -m reconcile.qc_reconcile --settle --session {expect}),"
            f"再重跑本日")
        return row

    row["prev"] = {"session": d_prev, "D_usd": pe["D_usd"],
                   "qc_cash": pe.get("qc_cash"),
                   "fractional_residual_usd":
                       (pe.get("fractional_residual") or {}).get("value_usd")}
    dD = D - float(pe["D_usd"])
    row["delta_D_usd"] = round(dD, 2)

    # —— 可归因项(全部按"对 ΔD 的贡献"符号写,便于直接相减)——
    attrib: dict = {}

    q = _queue_pnl(session, d_prev, built, snap, scalars)
    attrib["bootstrap_unmirrored_pnl"] = q          # 官方吃了、QC 没吃 ⇒ 推高 D

    slip = 0.0
    unref = []
    for f in fills:
        ref = f.get("submit_last_px")
        if ref is None:
            unref.append(f["ticker"])
            continue
        slip += (f["fill_px"] - float(ref)) * f["qty"]
    attrib["slippage"] = {
        "usd": round(slip, 2), "n_fills": len(fills),
        "basis": "fill_px vs orderSubmissionData.lastPrice(下单瞬间最后成交价)",
        "unreferenced": sorted(set(unref)),
        "note": "正数 = 成交价对我们不利,压低 QC 净值 ⇒ 推高 D"}

    # 换仓镜像滞后:面板按**决策日收盘价**(= d_prev 官方收盘)入账,QC 要等
    # 次日 target 应用后才成交接上仓位。[决策收盘 → 下单瞬间] 这段行情只有
    # 面板在场,是 ΔD 的**永久台阶**(2026-08-28 实测 −12.9k,其中 FTNT 隔夜
    # −4.8% 一腿就 −9.2k)。上界取**下单瞬间**而不是成交价:[下单 → 成交] 的
    # 尾段是上面 slippage 的地盘,算到成交价会把滑点双计。缺下单参考价的腿
    # 退回用成交价整窗归本项 —— slippage 对那种腿本来就算不了,不重不漏。
    # 决策收盘价用与 P/Q 同一价源(Polygon 日 K):面板账本自己的入账价可能
    # 略有出入,那点差留在 unattributed 里,不硬凑。
    lag, lag_rows, lag_unpriced = 0.0, [], []
    prev_closes: dict = {}
    if fills:
        try:
            prev_closes = official_close.closes_for(
                d_prev, [rolloff._canon(f["ticker"]) for f in fills])
        except SourceError:
            pass                       # 逐腿落 unpriced,blocked 里点名
    for f in fills:
        c0 = prev_closes.get(rolloff._canon(f["ticker"]))
        if c0 is None:
            lag_unpriced.append(f["ticker"])
            continue
        ref = f.get("submit_last_px")
        end = float(ref) if ref is not None else float(f["fill_px"])
        leg = (end - float(c0)) * f["qty"]
        lag += leg
        lag_rows.append({"ticker": f["ticker"], "qty": f["qty"],
                         "decision_close": float(c0), "end_px": end,
                         "full_window": ref is None, "usd": round(leg, 2)})
    attrib["rebalance_mirror_lag"] = {
        "usd": round(lag, 2), "n_legs": len(lag_rows),
        "basis": f"qty × (下单瞬间价 − {d_prev} 官方收盘价);"
                 f"缺下单价的腿用成交价整窗",
        "per_leg": lag_rows, "unpriced": sorted(set(lag_unpriced)),
        "note": "面板按决策日收盘入账、QC 次日成交才接上 —— 正数 = 接上前"
                "行情走高(面板吃到 QC 没吃)⇒ 推高 D;此项永久沉淀,冻结 K"
                "之后按日滚入 k_effective(见 k_effective())"}

    prev_frac = (pe.get("fractional_residual") or {}).get("value_usd")
    attrib["fractional_residual_delta"] = {
        "usd": (round(frac_usd - float(prev_frac), 2)
                if prev_frac is not None else None),
        "status": "ok" if prev_frac is not None else "no_prev"}

    # 股息时点项。QC 侧:现金变动里扣掉成交现金流剩下的部分(零费率配置下
    # 只剩股息;§2.2 借券费/保证金利息 paper 不计)。本地侧:五本 account 的
    # cumulative_dividends 日增量换算到官方口径。
    qc_div = None
    prev_cash = pe.get("qc_cash")
    # fillQuantity 带符号(卖单为负,2026-08-27 实测),所以 fill_cash 直接就是
    # 净现金流出。现金恒等式:Δcash = −fill_cash − fees + 股息 ⇒ 反解股息。
    # 手续费实测全 0(paper 零费率配置),但**不能**因此省掉这一项:哪天费率
    # 模型一变,费用就会整笔冒充成股息落进时点项里,而且没人会发现。
    fee_total = sum(f.get("fee") or 0.0 for f in fills)
    if prev_cash is not None:
        fill_cash = sum(f["fill_px"] * f["qty"] for f in fills)
        qc_div = (float(qc["cash"]) - float(prev_cash)) + fill_cash + fee_total
    loc_div, div_missing = 0.0, []
    prev_cum = (pe.get("cumulative_dividends") or {})
    cum = {}
    for st, a in acc.items():
        # 累计股息对**所有**在册账本都记(明天它入 P 时才有前日基准可比),
        # 但"缺基准"只对**进了 P** 的策略成立:QC 还没镜像的策略(如挂载前的
        # aeus)不在 P 里,它的股息本来就不该进时点项,更不该把裁决拦成 partial。
        cum[st] = float(a.get("cumulative_dividends") or 0.0)
        if st not in off:
            continue
        if st not in prev_cum:
            div_missing.append(st)
            continue
        k = 1.0 if st in PAIR_STRATEGIES else float((scalars or {}).get(st, 1.0))
        loc_div += (cum[st] - float(prev_cum[st])) * k
    row["cumulative_dividends"] = cum
    attrib["dividend_timing"] = {
        "qc_nonfill_cash_usd": None if qc_div is None else round(qc_div, 2),
        "fees_usd": round(fee_total, 2),
        "local_official_basis_usd": round(loc_div, 2),
        "usd": (None if qc_div is None else round(loc_div - qc_div, 2)),
        "missing_prev": sorted(div_missing),
        "transition_contaminated": bool(q["per_pair"]),
        "note": "本地按除息日入账、QC 按付息日到现金,差额即时点项;过渡期内 "
                "L/S 队列的股息也落在这里(QC 根本没持有那些腿)"}
    # 挂载台阶:QC 无活的入金通道,新策略是保证金建仓 —— 本场次起 P 多出该策略
    # 官方净值而 Q 不变,ΔD 里因此坐着一整个台阶。它必须和 lag/slip 一样进 known:
    # 它同时滚进 k_effective,两处**成对入账**才保住 Q + k_eff ≡ P。只滚 k_eff 不进
    # known,等于让判据去解释一笔没人告诉它的钱 —— 2026-09-02 AEUS 上车实测,
    # 那会把整笔 deposit_K 倒进 unattributed,在一本干净的账上报 ~1,490bp 假 breach。
    # 无条件计算(不放进 frozen 分支):K 未冻结的部署同样要归因。
    ob = onboard_step(session)
    if ob:
        attrib["onboarding_step"] = {
            "usd": round(ob, 2),
            "basis": "onboard_log[].deposit_K 落在 (上一场次收盘, 本场次收盘]",
            "note": "QC 无入金通道、保证金建仓 ⇒ P 多一个策略净值、Q 不变,"
                    "永久台阶;与 k_effective 是同一笔,成对入账"}
    row["attribution"] = attrib

    # —— 残差与裁决 ——
    known: list[float] = [q["total_usd"], slip, lag, ob]
    blocked: list[str] = []
    if q["unresolved"]:
        blocked.append(f"{len(q['unresolved'])} 对 L/S 队列盈亏取不到读数")
    # 缺下单参考价本身不再拦:那种腿已被镜像滞后项按成交价整窗吸收(见上),
    # 残差里不缺东西。真拦的是缺**决策日收盘价**的腿 —— 那才是算不出的窗口。
    if lag_unpriced:
        blocked.append(f"{len(set(lag_unpriced))} 腿缺 {d_prev} 官方收盘价,"
                       f"镜像滞后算不全")
    fd = attrib["fractional_residual_delta"]["usd"]
    if fd is None:
        blocked.append("上一份报告没有小数残差市值")
    else:
        known.append(fd)
    dv = attrib["dividend_timing"]["usd"]
    if dv is None:
        blocked.append("上一份报告没有 QC 现金,股息时点项算不出")
    else:
        known.append(dv)
        if div_missing:
            blocked.append(f"股息基准缺策略 {sorted(div_missing)}")

    # 判据一律用**未归因**残差:把能算的项全扣掉之后还剩什么。§5 的 5bp/3bp
    # 之别只是宽严不同(换仓日要求更严,因为那天每一项都该被执行数据解释),
    # 不是换一个被判的量 —— 无成交日若改判"ΔD − bootstrap",一个除息日就会
    # 把股息时点项当成漂移报警。
    unattr = dD - sum(known)
    after_boot = dD - q["total_usd"]
    tol = REBAL_TOL_BP if fills else STEADY_TOL_BP
    if gross > 0:
        bp = unattr / gross * 1e4
    else:
        # gross=0 意味着 QC 一票没持 —— 这本身就该报警。若让 bp 退成 0,
        # 分母退化会直接换来一个 ok,把"账户空了"演成"对得最准的一天"。
        bp = 0.0
        blocked.append("QC gross exposure = 0(账户无持仓),bp 判据分母退化")
    row.update(attributed_usd=round(sum(known), 2),
               unattributed_usd=round(unattr, 2),
               after_bootstrap_usd=round(after_boot, 2),
               judged_usd=round(unattr, 2),
               judged_basis=("unattributed;换仓日阈值 3bp" if fills
                             else "unattributed;无成交日阈值 5bp"),
               judged_bp_gross=round(bp, 2), tolerance_bp=tol,
               n_fills=len(fills))

    # 冻结后:K 不是死常数,换仓日的永久台阶按日滚入(k_effective)。
    # 当天自己的台阶(lag + slip)从内存加 —— 本 session 的报告此刻还没写盘。
    # D − k_effective 才是"真漂移":它只应剩股息时点等会自己回冲的项。
    if frozen and frozen.get("measured_on"):
        k_eff, n_steps, k_miss = k_effective(frozen, session)
        k_eff += lag + slip + ob      # ob 已在归因段算出并入 known,此处复用
        if ob:
            row["k_onboard_step_usd"] = round(ob, 2)   # 本场次挂载台阶(留痕)
        row["k_effective_usd"] = round(k_eff, 2)
        row["D_minus_K_effective_usd"] = round(D - k_eff, 2)
        row["k_steps_counted"] = n_steps + 1
        if k_miss:
            row["k_steps_missing"] = k_miss
            blocked.append(f"k_effective 缺 {len(k_miss)} 天的台阶"
                           f"(那些天归因未出): {k_miss}")
    if blocked:
        row["blocked_terms"] = blocked
    if abs(bp) > tol:
        row["status"] = "breach"
        row["note"] = (f"未归因残差 {unattr:+,.2f} = {bp:+.2f}bp/gross,"
                       f"超 {tol}bp({len(fills)} 笔成交)")
    elif blocked:
        # 阈值没破,但有项目根本没算进来 —— 不能给绿灯,否则漏项会被"看起来
        # 很小的残差"掩盖(漏的那项可能恰好与真误差抵消)。
        row["status"] = "partial"
        row["note"] = "阈值内,但有项目无法归因(见 blocked_terms),不出 ok"
    else:
        row["status"] = "ok"
    return row


# ── 主流程 ──────────────────────────────────────────────────────────────────

def reconcile(session: str | None = None, dry: bool = False) -> dict:
    et = rolloff._et_now()
    session = session or last_session(et)
    prior = _load(report_path(session), {}) or {}
    rep: dict = {
        "session": session,
        "generated_at": et.isoformat(timespec="seconds"),
        "generated_at_utc": _now_utc(),
        "passes": (prior.get("passes") or []) + [et.isoformat(timespec="seconds")],
        "method": "M4 QC↔local reconcile: ①holdings 0-diff vs pushed target "
                  "(gated on QC applied version) ②pushed vs rebuilt target "
                  "③NAV budget D=Σofficial_EOD−QC_equity, ΔD decomposed per "
                  "plan §5 + §9.2 three-queue transition",
        "verdict": "incomplete",
    }
    scalars = (_load(EXPORTER_STATE) or {}).get("scalars") or {}
    rep["scalars"] = scalars

    # 五持仓文件**只读一次**:target 对拍、过渡期队列、L/S 盈亏三处共用同一份
    # 快照。分别读会在盘中平仓落进两次读之间时产出自相矛盾的报表。
    import exporter as exp
    from inventory_source import read_snapshot
    snap = built = composed = None
    compose_err = None
    try:
        snap = read_snapshot()
        composed = exp.compose(snap)
        built = composed["built"]
    except SourceError as e:
        compose_err = str(e)

    c, pid = rolloff.qc_client_project()
    qc = rolloff.qc_snapshot(client=c, pid=pid)
    rep["qc"] = {k: qc[k] for k in ("holdings_mv", "cash", "equity",
                                    "equity_reported", "price_staleness_usd",
                                    "gross", "deploy_id")}
    rep["qc"]["n_positions"] = len(qc["shares"])
    applied = qc_applied_version(c, pid)
    orders = qc_orders(c, pid, qc.get("deploy_id"))
    fills = fills_of_session(orders, session, et.tzinfo)
    rep["fills"] = {"n": len(fills), "gross_notional_usd":
                    round(sum(abs(f["fill_px"] * f["qty"]) for f in fills), 2),
                    "detail": fills}

    rep["holdings_check"] = merge_section(prior.get("holdings_check"),
                                          holdings_plane(qc, applied))
    rep["target_check"] = merge_section(
        prior.get("target_check"),
        target_plane(composed) if composed else
        {"status": "incomplete", "note": f"重算 target 失败: {compose_err}"})

    # 过渡期状态(§9.2)
    ages = []
    today = datetime.strptime(session, "%Y-%m-%d").date()
    for st in PAIR_STRATEGIES:
        for queue, key in (("L", "legacy_alive"), ("S", "scaled_alive")):
            for it in ((built or {}).get(key) or {}).get(st, []):
                od = it.get("open_date")
                ages.append({"strategy": st, "queue": queue, "pair": it["pair"],
                             "open_date": od,
                             "age_days": ((today - datetime.strptime(
                                 od, "%Y-%m-%d").date()).days if od else None)})
    n_l = sum(1 for a in ages if a["queue"] == "L")
    n_s = sum(1 for a in ages if a["queue"] == "S")
    rep["bootstrap"] = {"transition": bool(ages), "legacy_remaining": n_l,
                        "scaled_remaining": n_s, "ages": ages,
                        "known": built is not None}
    if built is not None and not ages:
        rep["bootstrap"]["milestone"] = (
            "L/S 两队已清空 —— 退场条件满足;待某 session 三段全 ok 后跑 "
            "ops/rolloff.py --freeze --session <那天> 锚定冻结 K(报告锚定制,"
            "不要求账户静止)")

    # ── QC 侧收盘存档 ──────────────────────────────────────────────────────
    # D 日的 Q 只在 [16:00 D, 09:30 D+1) 可观测;D 日的官方 EOD 与 monitor_log
    # 都由 20:30 那趟 pipeline 写出,次日 ~10:15 才落地(git 历史逐提交核过:
    # perf json 末行恒定滞后一个交易日)。两个窗口不重叠 ⇒ 同一时刻凑不齐 P 和
    # Q。所以把 QC 侧冻在这里,次日 --settle 用存档补算。
    in_win, win_why = in_close_window(session, et)
    rep["close_window"] = {"ok": in_win, "why": win_why or "在收盘窗口内"}
    if in_win:
        rep["close_snapshot"] = {
            "taken_at": et.isoformat(timespec="seconds"),
            # shares/deploy_id 必须一起存档:次日 --settle 时 QC 那边已经是新一
            # 天的账户,拿不回 D 日收盘的逐票股数;没有股数就没法用官方收盘价独立
            # 复算 Q,只能信 QC 自报的一个总数,失去交叉验证。
            "qc": {k: qc[k] for k in ("holdings_mv", "cash", "equity",
                                      "equity_reported", "price_staleness_usd",
                                      "gross", "prices", "shares", "deploy_id")},
            "fills": fills,
            "scalars": scalars,
            "residual": _load(RESIDUAL_PATH) or {}}
        eq_fresh = equity_plane(session, qc, fills, built, snap, scalars)
    else:
        # 窗口外读到的 Q 是别的交易日的盘中值。既不能拿它出裁决,更不能让它
        # 覆盖已存档的收盘快照 —— 存档一旦被污染,次日 settle 会用它算出一个
        # 外表完全正常的假 D,没有任何东西会报错。原样保留旧存档。
        if prior.get("close_snapshot"):
            rep["close_snapshot"] = prior["close_snapshot"]
        eq_fresh = {"status": "pending", "session": session,
                    "note": f"不在收盘窗口内: {win_why}。此刻的 QC 读数不能当 "
                            f"{session} 的收盘净值;equity 段交给 --settle "
                            f"用收盘存档补算"}
    rep["equity_check"] = merge_section(prior.get("equity_check"), eq_fresh)
    _verdict(rep)
    _emit(rep, dry=dry)
    return rep


def _verdict(rep: dict) -> str:
    st_all = [(rep.get(k) or {}).get("status") for k in
              ("holdings_check", "target_check", "equity_check")]
    if "breach" in st_all:
        rep["verdict"] = "breach"
    elif all(s == "ok" for s in st_all):
        rep["verdict"] = "ok"
    elif "ok" in st_all:
        rep["verdict"] = "partial"
    else:
        rep["verdict"] = "incomplete"
    return rep["verdict"]


def settle(session: str | None = None, dry: bool = False) -> dict:
    """次日补算 equity 段:QC 侧用收盘存档,本地侧用此刻(才齐)的数据。

    这趟**一行 QC API 都不调**,连客户端都不创建 —— 因此在结构上不可能推
    target、不可能下单。它只碰 equity 段,holdings/target 两段原样保留(那两段
    的现场在收盘那趟已经取过,此刻的 QC 持仓属于新一个交易日,拿来覆盖会把
    别人家的数写进 session 的报告)。
    """
    et = rolloff._et_now()
    session = session or last_session(et)
    rep = _load(report_path(session))
    if not rep:
        raise SourceError(
            f"没有 {session} 的报告可补算 —— 收盘那趟(16:20/23:15)没跑成,"
            f"那天的 QC 收盘净值已经错过,事后补不回来")
    cs = rep.get("close_snapshot")
    if not cs:
        raise SourceError(
            f"{session} 的报告里没有 close_snapshot —— 收盘窗口内没取到 QC 快照;"
            f"P 与 Q 不同源,不出裁决")
    prior_eq = rep.get("equity_check") or {}
    if prior_eq.get("status") in TERMINAL:
        print(f"[qc_reconcile] {session} equity 段已是终态"
              f"({prior_eq['status']}) — 幂等跳过")
        return rep

    import exporter as exp
    from inventory_source import read_snapshot
    try:
        snap = read_snapshot()
        built = exp.compose(snap)["built"]
    except SourceError as e:
        fresh = {"status": "pending", "session": session,
                 "note": f"持仓文件读不出来: {e} —— 过渡期未镜像盈亏算不了"}
    else:
        fresh = equity_plane(session, cs["qc"], cs["fills"], built, snap,
                             cs["scalars"], residual=cs.get("residual"))
        fresh["q_source"] = {"basis": "D 日收盘存档(本趟未读 QC)",
                             "taken_at": cs["taken_at"]}
    rep["equity_check"] = merge_section(prior_eq, fresh)
    rep["passes"] = (rep.get("passes") or []) + [et.isoformat(timespec="seconds")]
    rep["settled_at"] = et.isoformat(timespec="seconds")
    _verdict(rep)
    _emit(rep, dry=dry)
    return rep


def settle_all(dry: bool = False, include_current: bool = True) -> int:
    """不带 --session 的补算:把**还欠着的**每一天按时间顺序补完,不只补今天。

    返回进程退出码(2=有 breach,1=有真失败,0=都好)。breach 压过失败:前者是
    钱对不上,后者多半是某天补不回来了 —— 两条都会原样打出来,不互相掩盖。
    """
    et = rolloff._et_now()
    todo = unsettled_sessions(et)
    if include_current:
        cur = last_session(et)
        # 当天那份即便报告缺失/没快照也要走一遍 —— 那两种情况该炸出来,
        # 不能因为它进不了 unsettled_sessions 就变成"今天没活干"。
        # sorted:补算必须**从旧到新**,ΔD 链是逐日接的,顺序反了就接不上。
        todo = sorted(set(todo) | {cur})
    else:
        # live 那趟的收尾:当天的现场刚由 reconcile() 取过,别再拿存档覆盖。
        todo = [s for s in todo if s != last_session(et)]
    if not todo:
        print("[qc_reconcile] 没有欠账可补算")
        return 0
    if len(todo) > 1:
        print(f"[qc_reconcile] 待补算 {len(todo)} 天: {', '.join(todo)}")
    rc, failed, breached = 0, [], []
    for s in todo:
        try:
            rep = settle(s, dry=dry)
        except SourceError as e:
            print(f"!!!! [qc_reconcile] {s}: {e}")
            failed.append(s)
            continue
        if rep.get("verdict") == "breach":
            breached.append(s)
    if breached:
        rc = 2
    elif failed:
        rc = 1
    if failed or breached:
        print(f"[qc_reconcile] 补算小结: breach={breached or '无'} "
              f"失败={failed or '无'}")
    return rc


def _emit(rep: dict, dry: bool = False) -> None:
    if not dry:
        rolloff._atomic_write(report_path(rep["session"]), rep)
    print(f"[qc_reconcile] {rep['session']} verdict={rep['verdict']}"
          + ("  (dry — 未写文件)" if dry else
             f" -> {report_path(rep['session']).name}"))
    h, t, e = (rep["holdings_check"], rep["target_check"], rep["equity_check"])
    print(f"  ① holdings  [{h['status'].upper()}] "
          + (f"{h.get('n_matched')}/{h.get('n_tickers')} 逐票相符"
             if "n_tickers" in h else h.get("note", "")))
    for d in (h.get("diffs") or [])[:10]:
        print(f"       {d['ticker']:<6} QC {d['qc']:>9,}  target {d['target']:>9,}"
              f"  差 {d['diff']:+,}")
    print(f"  ② target    [{t['status'].upper()}] "
          f"pushed={t.get('pushed_hash')} rebuilt={t.get('rebuilt_hash')}"
          + (f"  {t['n_diff']} 票不一致" if t.get("n_diff") else ""))
    print(f"  ③ equity    [{e['status'].upper()}]")
    if e.get("D_usd") is not None:
        print(f"       P(官方Σ) {e['official_total_P']:>15,.2f}   "
              f"Q(QC净值) {e['qc_equity_Q']:>15,.2f}   D {e['D_usd']:>13,.2f}")
    if e.get("delta_D_usd") is not None:
        q = e["attribution"]["bootstrap_unmirrored_pnl"]
        ml = (e["attribution"].get("rebalance_mirror_lag") or {}).get("usd", 0.0)
        print(f"       ΔD {e['delta_D_usd']:>+13,.2f}  = 过渡项 "
              f"{q['total_usd']:+,.2f} + 镜像滞后 {ml:+,.2f} + 滑点 "
              f"{e['attribution']['slippage']['usd']:+,.2f} + 其他 → "
              f"残差 {e['judged_usd']:+,.2f} = {e['judged_bp_gross']:+.2f}bp "
              f"(阈值 {e['tolerance_bp']}bp, {e['n_fills']} 笔成交)")
    if e.get("k_effective_usd") is not None:
        print(f"       K_eff {e['k_effective_usd']:>+13,.2f}"
              f"(台阶 {e.get('k_steps_counted')} 天)   "
              f"D − K_eff = {e['D_minus_K_effective_usd']:+,.2f}")
    if e.get("note"):
        print(f"       note: {e['note']}")
    b = rep["bootstrap"]
    print(f"  过渡期: L={b['legacy_remaining']} S={b['scaled_remaining']}"
          + (f"  {b['milestone']}" if b.get("milestone") else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="M4 QC↔本地 日终对账")
    ap.add_argument("--session", default=None, metavar="YYYY-MM-DD",
                    help="交易日;不传=最近一个已收盘交易日(NYSE 日历)")
    ap.add_argument("--dry", action="store_true", help="只打印,不写报告文件")
    ap.add_argument("--settle", action="store_true",
                    help="次日补算模式:用收盘存档的 QC 数据补 equity 段。"
                         "不创建 QC 客户端,不调用任何 QC API")
    ap.add_argument("--backlog", action="store_true",
                    help="只补算**欠账**(还没出终态、但存着收盘快照的往日),"
                         "跳过 last_session 那天。给 live 那趟收尾用")
    a = ap.parse_args()
    # --settle / --backlog 不带 --session:补**所有**欠着的天,不只是今天。
    # wrapper 只按 last_session 派活,官方 EOD 一拖过次日 13:30,前一天就再也
    # 派不到活了 —— 派活口径必须是"还欠着什么",不是"今天是哪天"。
    if (a.settle or a.backlog) and a.session is None:
        try:
            return settle_all(dry=a.dry, include_current=not a.backlog)
        except SourceError as e:
            print(f"!!!! [qc_reconcile] {e}")
            return 1
    try:
        rep = settle(a.session, dry=a.dry) if a.settle \
            else reconcile(a.session, dry=a.dry)
    except SourceError as e:
        print(f"!!!! [qc_reconcile] {e}")
        return 1
    return 2 if rep["verdict"] == "breach" else 0


if __name__ == "__main__":
    sys.exit(main())
