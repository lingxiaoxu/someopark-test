# SomeoPark M0 Probe — 一次性平台行为实测(paper,周一开盘跑)
#
# R1 小数股: 0.5 股 BIL 市价单是否被 LotSize 拒
# R6 零费:  SetFeeModel(ConstantFeeModel(0)) 后 1 股成交的 OrderFee 是否为 0
# R2 传输:  API 推送的 ObjectStore 键在算法端可读性 + 读延迟
# 全部结果打 [M0-RESULT] 日志;跑完自我停牌(不再下单),由 ops/stop.py 停机。
from AlgorithmImports import *  # noqa: F401,F403
import json

TARGET_KEY = "mirror/target.json"
DONE_KEY = "mirror/m0_done.json"


class SomeoParkM0Probe(QCAlgorithm):

    def Initialize(self):
        self.SetTimeZone("America/New_York")
        self.SetStartDate(2026, 8, 17)
        self.SetCash(100000)
        self.SetBrokerageModel(BrokerageName.QuantConnectBrokerage)
        self.SetSecurityInitializer(
            lambda s: s.SetFeeModel(ConstantFeeModel(0)))
        self.bil = self.AddEquity("BIL", Resolution.Minute).Symbol
        self.done = self.ObjectStore.ContainsKey(DONE_KEY)
        self.Schedule.On(self.DateRules.EveryDay(self.bil),
                         self.TimeRules.AfterMarketOpen(self.bil, 2),
                         self.RunProbe)
        self.Debug(f"[M0] init, done={self.done}")

    def RunProbe(self):
        if self.done:
            return
        # R2: ObjectStore 读(API 侧已推 target)
        try:
            if self.ObjectStore.ContainsKey(TARGET_KEY):
                doc = json.loads(self.ObjectStore.Read(TARGET_KEY))
                self.Log(f"[M0-RESULT] R2 objectstore_read=OK "
                         f"version={doc.get('version')} "
                         f"exported_at={doc.get('exported_at')} "
                         f"n_targets={len(doc.get('targets', {}))}")
            else:
                self.Log("[M0-RESULT] R2 objectstore_read=KEY_MISSING "
                         "(exporter 未推送或键不共享 — 需排查)")
        except Exception as e:                              # noqa: BLE001
            self.Log(f"[M0-RESULT] R2 objectstore_read=ERROR {e}")

        # R6: 零费验证(1 股 BIL)
        t1 = self.MarketOrder(self.bil, 1, tag="[M0] R6 fee probe")
        self.Log(f"[M0-RESULT] R6 submit status={t1.Status}")

        # R1: 小数股(0.5 股 BIL)
        try:
            t2 = self.MarketOrder(self.bil, 0.5, tag="[M0] R1 fractional probe")
            self.Log(f"[M0-RESULT] R1 fractional submit status={t2.Status} "
                     f"(Invalid=拒绝, 其它=接受)")
            if t2.Status == OrderStatus.Invalid:
                self.Log(f"[M0-RESULT] R1 error="
                         f"{t2.SubmitRequest.Response.ErrorMessage}")
        except Exception as e:                              # noqa: BLE001
            self.Log(f"[M0-RESULT] R1 fractional raise={e}")

        self.done = True
        self.ObjectStore.Save(DONE_KEY, json.dumps({"at": str(self.Time)}))
        self.Log("[M0-RESULT] probe complete — 可 stop.py --liquidate 收尾")

    def OnOrderEvent(self, e):
        if e.Status == OrderStatus.Filled:
            fee = e.OrderFee.Value.Amount if e.OrderFee else None
            self.Log(f"[M0-RESULT] fill {e.Symbol.Value} qty={e.FillQuantity} "
                     f"@ {e.FillPrice} fee={fee} "
                     f"(R6 期望 fee=0.0)")
