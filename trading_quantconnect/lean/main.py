# SomeoPark Mirror — QC 哑目标态执行器(plan v2;零策略逻辑)
#
# 职责: 读 ObjectStore 的 mirror/target.json(本地 exporter 推送),把账户
# 调到 target 状态。时序状态机: RTH 内新版本立即执行;盘前/盘后/节假日
# 挂起至下一开盘(QC 交易所日历唯一权威)。版本号幂等,重启自愈。
# 防火墙: 本算法绝不产生任何回流(不写策略侧任何东西;唯一写=自身状态
# mirror/last_applied.json 于 ObjectStore)。
from AlgorithmImports import *  # noqa: F401,F403
import json

TARGET_KEY = "mirror/target.json"
APPLIED_KEY = "mirror/last_applied.json"
STALE_WARN_MIN = 20
POLL_TAG = "[MIRROR]"


class SomeoParkMirror(QCAlgorithm):

    def Initialize(self):
        self.SetTimeZone("America/New_York")
        self.SetStartDate(2026, 8, 17)           # live 忽略,回测兜底
        self.SetCash(100000)                     # live paper 以部署现金为准;首版由 C0 校准
        self.SetBrokerageModel(BrokerageName.QuantConnectBrokerage)
        self.SetSecurityInitializer(self._init_security)
        self.spy = self.AddEquity("SPY", Resolution.Minute).Symbol  # 日历/调度锚
        self.pending = False
        self.target_doc = None
        self.applied_version = self._load_applied()
        self.cash_initialized = self.applied_version > 0
        self._last_stale_warn = self.Time
        # 每分钟拉取+状态机;开盘时刻单独钩子(9:30 整执行 pending)
        self.Schedule.On(self.DateRules.EveryDay(self.spy),
                         self.TimeRules.Every(timedelta(minutes=1)),
                         self.PollAndAct)
        self.Schedule.On(self.DateRules.EveryDay(self.spy),
                         self.TimeRules.AfterMarketOpen(self.spy, 0),
                         self.OnOpen)
        self.Debug(f"{POLL_TAG} init: applied_version={self.applied_version}")

    def _init_security(self, security):
        security.SetFeeModel(ConstantFeeModel(0))           # 对齐本地零费口径
        try:
            security.SetMarginModel(SecurityMarginModel(2.0))
        except Exception as e:                              # noqa: BLE001
            self.Debug(f"{POLL_TAG} margin model set failed {security.Symbol}: {e}")

    # ── 状态持久化(ObjectStore,重启自愈)────────────────────────────────────
    def _load_applied(self) -> int:
        try:
            if self.ObjectStore.ContainsKey(APPLIED_KEY):
                return int(json.loads(self.ObjectStore.Read(APPLIED_KEY))["version"])
        except Exception as e:                              # noqa: BLE001
            self.Error(f"{POLL_TAG} applied-state read failed: {e}")
        return 0

    def _save_applied(self, version: int):
        self.ObjectStore.Save(APPLIED_KEY, json.dumps(
            {"version": version, "at": str(self.Time)}))

    # ── target 读取与强校验(损坏拒绝应用,绝不部分应用)──────────────────────
    def _read_target(self):
        if not self.ObjectStore.ContainsKey(TARGET_KEY):
            return None
        try:
            doc = json.loads(self.ObjectStore.Read(TARGET_KEY))
            assert doc.get("schema") == 1
            assert isinstance(doc.get("version"), int)
            t = doc.get("targets")
            assert isinstance(t, dict) and all(
                isinstance(k, str) and isinstance(v, int) for k, v in t.items())
            return doc
        except Exception as e:                              # noqa: BLE001
            self.Error(f"{POLL_TAG} TARGET REJECTED (schema/corrupt): {e}")
            return None

    # ── 状态机 ──────────────────────────────────────────────────────────────
    def PollAndAct(self):
        doc = self._read_target()
        if doc is None:
            return
        self.target_doc = doc
        if doc["version"] <= self.applied_version:
            return
        if self._market_open():
            self._apply(doc)                     # RTH: 立即(含 10am 迟到变更)
        else:
            if not self.pending:
                self.Debug(f"{POLL_TAG} v{doc['version']} queued for next open")
            self.pending = True                  # 盘前/盘后/假日: 顺延

    def OnOpen(self):
        # 开盘: 重新拉最新(隔夜可能多版,只执行最新)
        doc = self._read_target()
        if doc is not None:
            self.target_doc = doc
        if self.target_doc and self.target_doc["version"] > self.applied_version:
            self._apply(self.target_doc)
        self.pending = False

    def _market_open(self) -> bool:
        return self.Securities[self.spy].Exchange.Hours.IsOpen(self.Time, False)

    def _apply(self, doc):
        """收敛循环(深检修复 2026-08-17): 新订阅票同 tick 无行情/在途单
        → 跳过等下一轮;**全部 delta 清零才标记版本** —— 部分被拒不再丢腿,
        每分钟 Poll 自动重入直到收敛(diff 语义幂等,重入安全)。"""
        from mirror_logic import plan_orders
        if not self.cash_initialized:
            self._init_cash(doc)
        targets = {k: int(v) for k, v in doc["targets"].items()}
        attribution = doc.get("attribution", {})
        # 目标里的新票先订阅(数据下一分钟到,本轮它们会进 blocked)
        for tkr in targets:
            sym = Symbol.Create(tkr, SecurityType.Equity, Market.USA)
            if not self.Securities.ContainsKey(sym):
                self.AddEquity(tkr, Resolution.Minute)

        holdings, blocked = {}, set()
        universe = set(targets)
        for kvp in self.Portfolio:
            if kvp.Key == self.spy:
                continue
            if kvp.Value.Invested:
                universe.add(kvp.Key.Value)
        for tkr in universe:
            sym = Symbol.Create(tkr, SecurityType.Equity, Market.USA)
            holdings[tkr] = int(self.Portfolio[sym].Quantity)
            sec = self.Securities[sym] if self.Securities.ContainsKey(sym) else None
            if sec is None or sec.Price <= 0:
                blocked.add(tkr)                 # 尚无行情(刚订阅)
            elif len(self.Transactions.GetOpenOrders(sym)) > 0:
                blocked.add(tkr)                 # 在途单,等成交

        orders, converged = plan_orders(targets, holdings, blocked)
        for tkr, delta, kind in orders:
            sym = Symbol.Create(tkr, SecurityType.Equity, Market.USA)
            tag = (f"{POLL_TAG} v{doc['version']} {kind} "
                   f"{json.dumps(attribution.get(tkr, {}), sort_keys=True)}")
            ticket = self.MarketOrder(sym, delta, tag=tag[:190])
            if ticket.Status == OrderStatus.Invalid:
                self.Error(f"{POLL_TAG} ORDER INVALID {tkr} Δ{delta}: "
                           f"{ticket.SubmitRequest.Response.ErrorMessage} "
                           f"— 单票下一轮重试,其余照常(F6)")
        if orders or blocked:
            self.Log(f"{POLL_TAG} v{doc['version']} converging: "
                     f"{len(orders)} orders, blocked={sorted(blocked)[:6]}")
        if converged:
            self.applied_version = doc["version"]
            self._save_applied(doc["version"])
            self.Log(f"{POLL_TAG} applied v{doc['version']} CONVERGED "
                     f"({len(targets)} target tickers)")

    def _init_cash(self, doc):
        c0 = doc.get("initial_cash")
        if c0:
            cur = float(self.Portfolio.CashBook["USD"].Amount)
            delta = float(c0) - cur
            if abs(delta) > 1:
                self.Portfolio.CashBook["USD"].AddAmount(delta)
                self.Log(f"{POLL_TAG} cash init: {cur:,.0f} → {float(c0):,.0f} "
                         f"(C0 = Σ account equity, plan §2.2)")
        self.cash_initialized = True

    def OnData(self, data):
        pass                                     # 全部逻辑在调度里;无策略逻辑

    def OnOrderEvent(self, e):
        if e.Status in (OrderStatus.Filled, OrderStatus.Canceled,
                        OrderStatus.Invalid):
            self.Log(f"{POLL_TAG} order {e.OrderId} {e.Status} "
                     f"{e.Symbol.Value} qty={e.FillQuantity} @ {e.FillPrice}")
