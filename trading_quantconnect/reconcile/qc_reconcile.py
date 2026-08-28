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
# 官方 EOD 与 QC 快照必须是同一个收盘的两侧。QC 侧"静没静"沿用 rolloff 的判据
# (runtimeStatistics.Equity 与自算净值两个采样的差)。
QUIET_TOL = rolloff.TOL_QUIET

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


# ── QC 只读取数 ─────────────────────────────────────────────────────────────

def qc_orders(c, pid, page: int = 100, hard_cap: int = 5000) -> list[dict]:
    """全部历史订单(分页)。

    live/orders/read 首次调用会返回 {"status":"loading","progress":0.0} —— QC
    在后台准备结果,要轮询到 payload 里出现 orders 键。实测(2026-08-27)第一
    次 loading、第二次即就绪。不轮询就会静默拿到空单列表 → 把换仓日误判成
    无成交日 → 用错阈值(5bp 而非 3bp)。
    """
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
            return out
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


def _ledger_accounts(session: str) -> tuple[dict, list[str]]:
    """五本 account json(须 as_of == session,否则该策略列进 stale)。"""
    acc, stale = {}, []
    for st, rel in LEDGER_ACCOUNT_FILES.items():
        d = stable_read(REPO / rel)
        if str(d.get("as_of")) != session:
            stale.append(f"{st}:as_of={d.get('as_of')}")
        acc[st] = d
    return acc, stale


def equity_plane(session: str, qc: dict, fills: list[dict], built: dict | None,
                 snap: dict | None, scalars: dict) -> dict:
    row: dict = {"session": session}
    if built is None or snap is None:
        return {"status": "pending", "session": session,
                "note": "持仓文件快照读不出来(见 target_check)—— 过渡期未镜像"
                        "盈亏算不了,不出净值裁决"}
    # 官方 EOD:五策略必须同一天(official_eod 自带这道闸门),且必须就是 session。
    try:
        d_off, off = rolloff.official_eod()
    except SourceError as e:
        return {"status": "pending", "session": session,
                "note": f"官方 EOD 不可用: {e}"}
    if d_off != session:
        return {"status": "pending", "session": session, "official_date": d_off,
                "note": f"官方 EOD 末行还停在 {d_off},不是 {session} —— 夜间 "
                        f"pipeline(21:30 ET)未跑完。拿 {d_off} 的官方净值去对 "
                        f"{session} 收盘的 QC 净值,量出来的是一整天行情不是"
                        f"对账误差,故不出裁决(等 23:15 那趟补)"}
    gap = qc.get("quiet_gap")
    if gap is not None and abs(gap) > QUIET_TOL:
        return {"status": "pending", "session": session,
                "quiet_gap": round(gap, 2),
                "note": f"QC 两个净值采样仍差 {gap:+,.2f}(阈值 ±{QUIET_TOL:,.0f})"
                        f" —— 账户没静下来(盘中/有单在飞),此刻的 Q 不是收盘态"}
    P = sum(off.values())
    Q = float(qc["equity"])
    D = P - Q
    gross = float(qc["gross"])
    row.update(official_date=d_off,
               official_eod={s: round(v, 2) for s, v in off.items()},
               official_total_P=round(P, 2), qc_equity_Q=round(Q, 2),
               qc_holdings_mv=round(qc["holdings_mv"], 2),
               qc_cash=round(qc["cash"], 2), gross_exposure=round(gross, 2),
               D_usd=round(D, 2))
    frozen = _load(ROLLOFF_PATH)
    if frozen:
        row["k_frozen"] = float(frozen["k_equity"])
        row["D_minus_K_usd"] = round(D - float(frozen["k_equity"]), 2)

    # 小数残差市值:官方口径持有 x 股,QC 只持 round(x) 股,差额靠 QC 自己
    # 报的现价定值(QC 没持有的票没有价 → 逐票列进 unpriced,不硬凑)。
    res = (_load(RESIDUAL_PATH) or {}).get("residual") or {}
    px = qc.get("prices") or {}
    frac_usd, unpriced = 0.0, []
    for t, r in res.items():
        p = px.get(t)
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
        cum[st] = float(a.get("cumulative_dividends") or 0.0)
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
    row["attribution"] = attrib

    # —— 残差与裁决 ——
    known: list[float] = [q["total_usd"], slip]
    blocked: list[str] = []
    if q["unresolved"]:
        blocked.append(f"{len(q['unresolved'])} 对 L/S 队列盈亏取不到读数")
    if unref:
        blocked.append(f"{len(set(unref))} 票成交缺下单瞬间参考价")
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
                                    "equity_reported", "quiet_gap", "gross",
                                    "deploy_id")}
    rep["qc"]["n_positions"] = len(qc["shares"])
    applied = qc_applied_version(c, pid)
    orders = qc_orders(c, pid)
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
            "L/S 两队已清空 —— 退场条件满足,可在 17:00–04:00 ET 静止窗口跑 "
            "ops/rolloff.py --freeze 焊死 K")

    rep["equity_check"] = merge_section(
        prior.get("equity_check"),
        equity_plane(session, qc, fills, built, snap, scalars))

    st_all = [rep[k].get("status") for k in
              ("holdings_check", "target_check", "equity_check")]
    if "breach" in st_all:
        rep["verdict"] = "breach"
    elif all(s == "ok" for s in st_all):
        rep["verdict"] = "ok"
    elif "ok" in st_all:
        rep["verdict"] = "partial"
    else:
        rep["verdict"] = "incomplete"

    _emit(rep, dry=dry)
    return rep


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
        print(f"       ΔD {e['delta_D_usd']:>+13,.2f}  = 过渡项 "
              f"{q['total_usd']:+,.2f} + 滑点 "
              f"{e['attribution']['slippage']['usd']:+,.2f} + 其他 → "
              f"残差 {e['judged_usd']:+,.2f} = {e['judged_bp_gross']:+.2f}bp "
              f"(阈值 {e['tolerance_bp']}bp, {e['n_fills']} 笔成交)")
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
    a = ap.parse_args()
    try:
        rep = reconcile(a.session, dry=a.dry)
    except SourceError as e:
        print(f"!!!! [qc_reconcile] {e}")
        return 1
    return 2 if rep["verdict"] == "breach" else 0


if __name__ == "__main__":
    sys.exit(main())
