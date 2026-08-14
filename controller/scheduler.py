"""
controller/scheduler.py — tick 循环 + 结构 watcher + 双引擎对拍 + 输出流(M4)。

循环(plan §四):
  watcher(独立节拍,7×24):WATCH_FILES 内容摘要变化 → 立即全量重建两引擎
    + last_price 合成重放(修 C)→ nav_latest 即刻反映新持仓(盘后也生效)。
  行情 tick(interval,仅开市):Polygon 批量快照 → 两引擎 apply → 容差对拍
    (发布=树引擎,verifier=拍平)→ 定期全量 rebaseline 审计 → 落盘。

输出(controller/output/,数据 gitignore、代码入库):
  nav_latest.json                 最新全层级值(前端轮询;原子写)
  nav_stream_{YYYYMMDD}.csv       ts,node_id,display_name,kind,value,stale 追加流
  risk_matrix_latest.json         拍平引擎反向索引
  structure_snapshot_{hash}.json  每次重建留痕
对拍:|Δ| ≤ max(1e-6, 1e-9·|v|) 视为相等;超差 abort 不发布(plan §4.2)。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import traceback
from datetime import datetime, timezone

from controller.registry import Registry
from controller.model import assemble, WATCH_FILES, REPO
from controller.engine_flatten import FlattenEngine
from controller.engine_tree import TreeEngine
from controller.prices import PriceFeed, et_today

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "output")

ABS_TOL, REL_TOL = 1e-6, 1e-9
REBASELINE_EVERY_S = 1800


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _et_hhmm() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M")


def _atomic_json(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, path)


def _watch_digest() -> str:
    h = hashlib.sha1()
    for rel in WATCH_FILES:
        p = os.path.join(REPO, rel)
        try:
            with open(p, "rb") as fh:
                h.update(hashlib.sha1(fh.read()).digest())
        except FileNotFoundError:
            h.update(b"MISSING:" + rel.encode())
    return h.hexdigest()[:16]


class EngineDisagreement(RuntimeError):
    pass


def _diff_structures(old_nodes: dict, new_nodes: dict, reg) -> list[str]:
    """相邻两份结构的人话摘要('MTFS 5→4' 式;plan §4.3 持仓变化提示)。"""
    out = []
    strategies = {nid for nid, n in {**old_nodes, **new_nodes}.items()
                  if n["kind"] == "strategy"}
    # 语言中立符号(直接进前端显示,不做 i18n):+ 新开 / − 平掉 / Δ shares 变
    for sid in sorted(strategies):
        o, n = old_nodes.get(sid), new_nodes.get(sid)
        name = reg.render(sid)
        if o is None or n is None:
            out.append(f"{name} {'+' if o is None else '−'}")
            continue
        oc = {c: q for c, q in o["children"]}
        nc = {c: q for c, q in n["children"]}
        if len(oc) != len(nc):
            out.append(f"{name} {len(oc)}→{len(nc)}")
        added = [reg.render(c) for c in nc.keys() - oc.keys()]
        removed = [reg.render(c) for c in oc.keys() - nc.keys()]
        resized = sum(1 for c in oc.keys() & nc.keys() if oc[c] != nc[c])
        if added:
            out.append(f"{name} + {', '.join(sorted(added))}")
        if removed:
            out.append(f"{name} − {', '.join(sorted(removed))}")
        if resized:
            out.append(f"{name} Δ{resized}")
    return out


class Controller:
    def __init__(self, feed: PriceFeed | None = None):
        self.reg = Registry()
        self.feed = feed if feed is not None else PriceFeed(self.reg)
        self.last_price: dict[str, float] = {}
        self.watch_digest: str | None = None
        self.last_rebaseline = 0.0
        self.last_rebuild_ts: str | None = None
        self.structure_diff: list[str] = []
        self._splits: dict[str, str] = {}      # {leaf_isin: "from:to"} 当日 split
        self._splits_date: str | None = None
        self._eod_done_date: str | None = None  # 官方收盘补写完成的 ET 日
        self.S = None                          # 无一致结构前不发布
        self.rebuild_error: str | None = None
        self._failed_digest: str | None = None
        self._failed_at = 0.0
        # tick 级 fail-not-die(2026-08-14): 一次瞬时 DNS 失败曾把常驻循环整个打挂
        # (8/13 14:04 market_status → NameResolutionError,净值冻结 ~22h)。
        # 常驻进程的不变量: **任何单轮异常都不得终止循环**——标错、沿用上轮、continue。
        self.tick_error: str | None = None
        self._tick_failed_at = 0.0
        self.tick_fail_streak = 0
        self.last_status: dict | None = None   # market_status 失败时的沿用值
        # 日内盈亏 = 纯美元账(shares × 价格,零比值;用户令 2026-08-12):
        #   day_pnl[nid] = realized[nid] + Σ eff_shares × (p_now − basis)
        #   basis = 日初价格(隔夜 carry)或当日开仓成交价(持仓文件 open_sX_price)
        #   换结构:旧持仓按换出时价格实现入 realized,新持仓从成交价起算
        #   day_return = day_pnl / 日初账面(仅归一化,不跨口径)
        self.day_date: str | None = None
        self.day_base_value: dict[str, float] = {}     # 日初账面(分母)
        self.day_realized: dict[str, float] = {}       # 已实现当日盈亏($)
        self.day_basis: dict[str, dict[str, float]] = {}  # {nid: {leaf: 基准价}}
        self._legacy_base: dict[str, float] | None = None
        self._load_day_state()
        self._maybe_rebuild(force=True)

    # ── 日内盈亏状态(重启不丢)───────────────────────────────────────────────
    def _day_state_path(self) -> str:
        return os.path.join(OUT_DIR, "day_state.json")

    def _load_day_state(self) -> None:
        try:
            st = json.load(open(self._day_state_path()))
            # eod 戳独立于日切判断:过期值(≠今天)天然不命中,无需清
            self._eod_done_date = st.get("eod_date")
            if st.get("date") != et_today():
                return
            if "base_value" in st:                      # 新格式
                self.day_date = st["date"]
                self.day_base_value = st["base_value"]
                self.day_realized = st["realized"]
                self.day_basis = st["basis"]
            elif "base" in st:                          # 旧比值格式:首 tick 换算
                self._legacy_base = st["base"]
        except (FileNotFoundError, ValueError, KeyError):
            pass

    def _save_day_state(self) -> None:
        _atomic_json({"date": self.day_date, "base_value": self.day_base_value,
                      "realized": self.day_realized, "basis": self.day_basis,
                      "eod_date": self._eod_done_date},
                     self._day_state_path())

    # ── 新结构下某节点的叶子基准价:当日开仓 pair 用真实成交价,其余用当前价 ────
    def _basis_for(self, nid: str, today: str) -> dict[str, float]:
        node = self.S.nodes[nid]
        entry_px: dict[str, float] = {}
        kids = node["children"]
        if node["kind"] == "pair" and node["attrs"].get("open_date") == today \
                and len(kids) == 2:
            a = node["attrs"]
            if a.get("open_s1_price"):
                entry_px[kids[0][0]] = float(a["open_s1_price"])
            if a.get("open_s2_price"):
                entry_px[kids[1][0]] = float(a["open_s2_price"])
        return {leaf: entry_px.get(leaf, self.last_price.get(leaf))
                for leaf in self.fe.expanded.get(nid, {})
                if entry_px.get(leaf) or leaf in self.last_price}

    def _day_pnl(self, nid: str) -> float | None:
        basis = self.day_basis.get(nid)
        if basis is None:
            return None
        pnl = self.day_realized.get(nid, 0.0)
        for leaf, eff in self.fe.expanded.get(nid, {}).items():
            p = self.last_price.get(leaf)
            if p is None:
                return None                     # 缺价不猜:该节点本 tick 无日内数
            b = basis.get(leaf)
            if b is None:                       # 建基时无价的叶子:首见价起算
                basis[leaf] = b = p
            pnl += eff * (p - b)
        return pnl

    # ── 结构(重)建 + 合成重放(修 C 统一语义)────────────────────────────────
    # 全部先在局部变量构好、验证通过才提交到 self——失败绝不留下半新半旧状态。
    def _rebuild(self, replay: bool = True) -> None:
        prev_nodes = self.S.nodes if self.S else None
        prev_expanded = dict(self.fe.expanded) if self.S else {}  # 旧持仓(实现日内盈亏用)
        # registry 每轮重读(2026-08-14):Registry 在 __init__ 只加载一次时,
        # `--build-master` 补完新票后**常驻进程看不见**,只能重启 —— rebuild_error
        # 里那句"run --build-master"的指引形同虚设(8/14 JNJ/PFE 新开仓卡住)。
        # 两次 json 读(~250 条)成本可忽略,让磁盘状态始终是唯一事实。
        self.reg = Registry()
        new_s = assemble(self.reg)
        fe, te = FlattenEngine(new_s), TreeEngine(new_s)
        init_f, init_t = fe.initial_emissions(), te.initial_emissions()
        if init_f != init_t:
            raise EngineDisagreement(f"init emissions differ: {init_f} vs {init_t}")
        if replay and self.last_price:
            replayable = {k: v for k, v in self.last_price.items()
                          if k in new_s.leaves()}
            if replayable:
                fe.apply_tick(replayable)
                te.apply_tick(replayable)
        self.S, self.fe, self.te = new_s, fe, te
        self._verify("rebuild-replay")
        # 换结构的日内盈亏衔接 = 纯美元账(零比值):
        # ① 旧持仓按换出时市场价"实现"当日至此的盈亏 → realized($,shares×价差);
        # ② 新持仓从基准价起算(当日开仓 pair 用持仓文件真实成交价,其余当前价)。
        # 记账台阶(现金重置/日切)天然不进 day_pnl——它从头到尾只有 shares×价格。
        if prev_expanded and self.day_date == et_today() and self.day_basis:
            today = et_today()
            for nid, basis in list(self.day_basis.items()):
                for leaf, eff in prev_expanded.get(nid, {}).items():
                    p, b = self.last_price.get(leaf), basis.get(leaf)
                    if p is not None and b is not None:
                        self.day_realized[nid] = \
                            self.day_realized.get(nid, 0.0) + eff * (p - b)
            for nid in self.fe.expanded:
                self.day_basis[nid] = self._basis_for(nid, today)
                self.day_realized.setdefault(nid, 0.0)
            self._save_day_state()
        self.structure_diff = (_diff_structures(prev_nodes, new_s.nodes, self.reg)
                               if prev_nodes else [])
        self.last_rebuild_ts = _now_iso()
        self.watch_digest = _watch_digest()
        snap_path = os.path.join(OUT_DIR, f"structure_snapshot_{self.S.hash}.json")
        if not os.path.exists(snap_path):
            _atomic_json({"hash": self.S.hash, "ts": _now_iso(),
                          "nodes": self.S.nodes,
                          "sources": self.S.sources}, snap_path)
        print(f"[controller] structure {'re' if replay else ''}built "
              f"hash={self.S.hash} nodes={len(self.S.nodes)} "
              f"leaves={len(self.S.leaves())}")

    # ── watcher 重建守门:失败不致死,沿用旧结构 + 标错 + 自动重试 ──────────────
    # 典型场景:盘中 pipeline 半更新窗(inventory 已写、account 未写)→ 装配
    # 正确拒绝;此处必须活着等文件齐(digest 再变或 120s 后重试),绝不裸奔崩溃。
    def _maybe_rebuild(self, force: bool = False) -> bool:
        dig = _watch_digest()
        if not force and dig == self.watch_digest:
            return False
        if not force and dig == self._failed_digest and \
                time.time() - self._failed_at < 120:
            return False                       # 同一失败状态,稍后再试
        try:
            self._rebuild(replay=self.S is not None)
            self.rebuild_error, self._failed_digest = None, None
            return True
        except Exception as e:  # noqa: BLE001 — 保守失败:服务旧结构
            self.rebuild_error = str(e)
            self._failed_digest, self._failed_at = dig, time.time()
            print(f"!!!! [controller WARN] rebuild failed — "
                  f"{'no structure yet' if self.S is None else 'serving last consistent structure'}"
                  f": {e}")
            return False

    # ── 对拍 + rebaseline ─────────────────────────────────────────────────────
    def _verify(self, ctx: str) -> None:
        vf, vt = self.fe.values(), self.te.values()
        if set(vf) != set(vt):
            raise EngineDisagreement(f"[{ctx}] priced sets differ")
        for k in vf:
            tol = max(ABS_TOL, REL_TOL * abs(vt[k]))
            if abs(vf[k] - vt[k]) > tol:
                raise EngineDisagreement(
                    f"[{ctx}] {k}: flatten={vf[k]!r} tree={vt[k]!r}")

    def _rebaseline(self) -> None:
        """第三重审计:直接全量求和,超容差以其为准重置两引擎(plan §4.2-3)。"""
        for nid in self.fe.order:
            if nid not in self.fe.fully_priced:
                continue
            direct = self.fe.cash_flat[nid] + sum(
                eff * self.last_price[leaf]
                for leaf, eff in self.fe.expanded[nid].items()
                if leaf in self.last_price)
            for eng, store in ((self.fe, self.fe.value), (self.te, self.te.total)):
                if abs(store[nid] - direct) > max(ABS_TOL, REL_TOL * abs(direct)):
                    print(f"!!!! [controller WARN] rebaseline {nid}: "
                          f"{eng.name}={store[nid]} direct={direct} — reset")
                    store[nid] = direct
                    eng.value_last_emitted[nid] = direct
        self.last_rebaseline = time.time()

    # ── 单轮 tick ─────────────────────────────────────────────────────────────
    def tick(self, force: bool = False) -> dict:
        # 1) watcher 兜底检查(独立轮询之外,tick 前必查;失败沿用旧结构)
        rebuilt = self._maybe_rebuild()
        if self.S is None:                     # 启动至今无一致结构:不发布
            return {"skipped": f"no consistent structure yet: {self.rebuild_error}",
                    "rebuilt": rebuilt}

        # 2) 市场状态(失败沿用上轮;冷启动无上轮 → 按 closed 保守处理:
        #    closed 只会让本轮走平移续写,不会污染价格)
        try:
            status = self.feed.market_status()
            self.last_status = status
        except Exception as e:  # noqa: BLE001 — 行情日历失败不得杀循环
            status = dict(self.last_status or {"market": "closed"})
            status["degraded"] = str(e)
            print(f"!!!! [controller WARN] market_status failed: {e} — "
                  f"沿用 market={status['market']}")
        closed = status["market"] == "closed"
        today_et = et_today()
        if force and closed and self._eod_done_date == today_et:
            # 强制轮(结构重建)在闭市窗拉的是快照 lastTrade,会盖掉已补写的
            # 官方收盘 —— 打回补写标记,下一常规轮重新对准官方 EOD(自愈)。
            self._eod_done_date = None

        # 2b) 当日 split 日历(每 ET 日一次;plan §九-7:只标注,不改 shares)
        if self._splits_date != today_et:
            try:
                self._splits = self.feed.splits_today(sorted(self.S.leaves()))
                if self._splits:
                    print(f"!!!! [controller WARN] splits today: "
                          f"{ {self.reg.render(k): v for k, v in self._splits.items()} } "
                          f"— corp_action 标注,shares 以持仓文件为准")
            except Exception as e:  # noqa: BLE001 — 日历失败不阻塞估值
                print(f"!!!! [controller WARN] splits calendar failed: {e}")
                self._splits = {}
            self._splits_date = today_et

        # 2c) 收盘后官方日 bar 补写(口径修正 B,2026-08-14):订阅是 15 分钟
        # 延迟行情,16:00 截断的"收盘"实际是 ~15:45 的价。闭市后第一轮
        # (≥16:20 ET,日 bar 已生成)用官方 daily_close 覆写价格并照常发布,
        # 让盘后/隔夜平移续写落在官方收盘上,次日 day_pnl 的隔夜 carry 基准
        # 也随之对准。非交易日 daily_close 为空且无请求失败 → 记完成不空转;
        # 请求失败 → 不记完成,下轮重试(fail-not-die)。
        eod_px: dict[str, float] | None = None
        if (closed and not force and self.last_price
                and self._eod_done_date != today_et and _et_hhmm() >= "16:20"):
            fails_before = self.feed.consecutive_failures
            try:
                got = self.feed.daily_close(sorted(self.S.leaves()), today_et)
            except Exception as e:  # noqa: BLE001 — 补写失败不影响正常发布
                got = None
                print(f"!!!! [controller WARN] EOD daily_close failed: {e} — retry")
            if got is not None and (
                    got or self.feed.consecutive_failures == fails_before):
                self._eod_done_date = today_et
                self._save_day_state()
                if got:
                    eod_px = got
                    print(f"[controller] EOD official close applied "
                          f"({len(got)}/{len(self.S.leaves())} leaves)")

        # 3) 快照 → 双引擎 → 对拍
        # 闭市且已有价:不拉快照,平移续写(Robinhood 式匀速推进,零 API 开销,
        # 用户令 2026-08-12)——nav 流每 interval 照常落一行,价格不变即平线;
        # 开市/extended-hours/强制/冷启动 → 正常拉快照。
        stale = False
        fetch = force or (not closed) or not self.last_price
        if eod_px:
            snap = {"feed_delay_min": 0.0,
                    "missing": [l for l in self.S.leaves() if l not in eod_px]}
            prices = eod_px
        elif fetch:
            try:
                snap = self.feed.snapshot(sorted(self.S.leaves()))
                prices = snap["prices"]
            except Exception as e:  # noqa: BLE001 — 不换源:标 stale 沿用 last_price
                print(f"!!!! [controller WARN] snapshot failed: {e} — tick stale")
                snap = {"feed_delay_min": None, "missing": []}
                prices, stale = {}, True
        else:
            snap = {"feed_delay_min": None, "missing": []}
            prices = {}
        if prices:
            em_f = self.fe.apply_tick(prices)
            em_t = self.te.apply_tick(prices)
            if [n for n, _ in em_f] != [n for n, _ in em_t] or any(
                    abs(a[1] - b[1]) > max(ABS_TOL, REL_TOL * abs(b[1]))
                    for a, b in zip(em_f, em_t)):
                raise EngineDisagreement(f"tick emissions differ:\n{em_f}\n{em_t}")
            self.last_price.update(prices)
        elif fetch:
            # 拉了却空手(如 3:30am ET snapshot 重置窗)= 同 stale 语义;
            # 闭市不拉的平移续写不算 stale(market=closed 已说明一切)
            stale = True
            if not self.last_price:      # 冷启动且全无价:不发近空 nav,不覆盖旧文件
                return {"skipped": "no prices available (snapshot empty, "
                                   "no last_price to carry)", "rebuilt": rebuilt}
        self._verify("tick")
        if time.time() - self.last_rebaseline > REBASELINE_EVERY_S:
            self._rebaseline()

        # 4) 落盘(发布=树引擎值)
        ts = _now_iso()
        values = self.te.values()
        # 日内盈亏账维护:ET 日切 → 全量重置(basis=隔夜 carry 价);
        # 旧比值格式 → 一次性换算(realized=至今修正盈亏,basis=当前价);
        # 盘中新节点 → 以首值/当前价起算
        if self._legacy_base is not None:      # 旧格式(loader 已核对同日)优先换算
            self.day_date = today_et
            self.day_base_value = dict(self._legacy_base)
            self.day_realized = {nid: values[nid] - b
                                 for nid, b in self._legacy_base.items()
                                 if nid in values}
            self.day_basis = {nid: {leaf: self.last_price[leaf]
                                    for leaf in self.fe.expanded.get(nid, {})
                                    if leaf in self.last_price}
                              for nid in values}
            self._legacy_base = None
            self._save_day_state()
        elif self.day_date != today_et:        # ET 日切:全量重置
            self.day_date = today_et
            self.day_base_value = dict(values)
            self.day_realized = {nid: 0.0 for nid in values}
            self.day_basis = {nid: self._basis_for(nid, today_et)
                              for nid in self.fe.expanded if nid in values}
            self._save_day_state()
        else:
            fresh = [nid for nid in values if nid not in self.day_base_value]
            if fresh:
                for nid in fresh:
                    self.day_base_value[nid] = values[nid]
                    self.day_realized.setdefault(nid, 0.0)
                    self.day_basis.setdefault(
                        nid, self._basis_for(nid, today_et))
                self._save_day_state()
        parent_of: dict[str, str] = {}
        for pid, n in self.S.nodes.items():
            for c, _ in n["children"]:
                parent_of[c] = pid
        rows = []
        for nid in self.fe.order:                        # 拓扑序稳定输出
            if nid not in values:
                continue
            n = self.S.nodes[nid]
            exp = self.fe.expanded.get(nid, {})
            corp = any(leaf in self._splits for leaf in exp)
            holdings = None
            if n["kind"] != "portfolio":     # 股票级明细(前端层级展开到叶子)
                holdings = sorted(
                    ({"id": leaf, "name": self.reg.render(leaf),
                      "shares": round(eff, 4),
                      "value": round(eff * self.last_price[leaf], 2)}
                     for leaf, eff in exp.items() if leaf in self.last_price),
                    key=lambda h: -abs(h["value"]))
            pnl = self._day_pnl(nid)
            base = self.day_base_value.get(nid)
            day_r = (round(pnl / base, 6)
                     if pnl is not None and base and abs(base) > 1e3 else None)
            rows.append({"node_id": nid,
                         "display_name": self.reg.render(nid),
                         "kind": n["kind"], "value": round(values[nid], 2),
                         "parent_id": parent_of.get(nid),
                         "corp_action": corp,
                         "day_return": day_r,
                         "day_pnl": round(pnl, 2) if pnl is not None else None,
                         "holdings": holdings,
                         "positions_as_of": n["attrs"].get("positions_as_of")})
        payload = {"ts": ts, "structure_hash": self.S.hash, "stale": stale,
                   "last_rebuild_ts": self.last_rebuild_ts,
                   "rebuild_error": self.rebuild_error,
                   "rebuild_error_age_s": (round(time.time() - self._failed_at)
                                           if self.rebuild_error else None),
                   "structure_diff": self.structure_diff,
                   "corp_actions": {self.reg.render(k): v
                                    for k, v in self._splits.items()},
                   "tick_error": self.tick_error,
                   "tick_error_age_s": (round(time.time() - self._tick_failed_at)
                                        if self.tick_error else None),
                   "market_degraded": status.get("degraded"),
                   "feed_delay_min": snap.get("feed_delay_min"),
                   "missing": [self.reg.render(m) for m in snap.get("missing", [])],
                   "backfilled": [self.reg.render(b)
                                  for b in snap.get("backfilled", [])],
                   "market": status["market"], "nodes": rows}
        _atomic_json(payload, os.path.join(OUT_DIR, "nav_latest.json"))
        stream = os.path.join(OUT_DIR,
                              f"nav_stream_{datetime.now().strftime('%Y%m%d')}.csv")
        os.makedirs(OUT_DIR, exist_ok=True)
        header = ("ts,node_id,display_name,kind,value,stale,corp_action,"
                  "structure_hash,day_return\n")
        if os.path.exists(stream):                      # 旧 schema 文件轮转,不混写
            with open(stream) as fh:
                if fh.readline() != header:
                    n = 1                               # 唯一后缀:绝不覆盖已有轮转段
                    while os.path.exists(f"{stream}.v{n}"):
                        n += 1
                    os.replace(stream, f"{stream}.v{n}")
        new = not os.path.exists(stream)
        with open(stream, "a") as fh:
            if new:
                fh.write(header)
            for r in rows:
                dr = "" if r["day_return"] is None else r["day_return"]
                fh.write(f"{ts},{r['node_id']},{r['display_name']},"
                         f"{r['kind']},{r['value']},{int(stale)},"
                         f"{int(r['corp_action'])},{self.S.hash},{dr}\n")
        _atomic_json(self.fe.risk_matrix(),
                     os.path.join(OUT_DIR, "risk_matrix_latest.json"))
        root_v = values.get(self.S.root)
        return {"ts": ts, "rebuilt": rebuilt, "stale": stale,
                "portfolio": root_v, "n_nodes": len(rows),
                "feed_delay_min": snap.get("feed_delay_min")}

    # ── 常驻循环 ─────────────────────────────────────────────────────────────
    def run(self, interval_s: int = 60, watch_s: int = 5) -> None:
        print(f"[controller] loop start interval={interval_s}s watch={watch_s}s")
        next_tick = 0.0
        while True:
            now = time.time()
            # 常驻进程不变量: 单轮任何异常都只标错+沿用上轮,**绝不终止循环**。
            # (8/13 一次 DNS 解析失败杀死进程 → 净值冻结 22 小时。瞬时故障必须
            #  自愈;真错误靠 tick_error 暴露给前端,而不是靠进程消失。)
            try:
                if self._maybe_rebuild():                 # 7×24 watcher(盘后也跑)
                    # 盘后结构变化也要立即反映到 nav_latest(修 C + 用户"马上变")
                    self.tick(force=True)
                if now >= next_tick:
                    out = self.tick()
                    if "skipped" not in out:
                        print(f"[tick] {out['ts']} portfolio="
                              f"{out['portfolio']:,.2f} stale={out['stale']}"
                              + (" REBUILD-ERR" if self.rebuild_error else ""))
                    next_tick = now + interval_s
                if self.tick_error:                       # 本轮走通 → 自愈
                    print(f"[controller] tick recovered after "
                          f"{self.tick_fail_streak} failure(s)")
                    self.tick_error, self.tick_fail_streak = None, 0
            except KeyboardInterrupt:
                raise
            except BaseException as e:  # noqa: BLE001 — 含 SystemExit:常驻不可被单轮杀死
                self.tick_fail_streak += 1
                self.tick_error = f"{type(e).__name__}: {e}"
                self._tick_failed_at = time.time()
                next_tick = now + interval_s              # 不空转重试
                print(f"!!!! [controller ERROR] tick failed "
                      f"(streak={self.tick_fail_streak}): {self.tick_error}")
                traceback.print_exc()
            time.sleep(watch_s)
