# mirror_logic — 镜像执行器的纯函数核心(零 LEAN 依赖,本地可单测)
#
# 深检修复(2026-08-17): 旧 _apply 在提交后立即标记版本 applied ——
# 新订阅票同 tick 无行情、订单被拒后**永不重试**,go-live 建仓必翻车。
# 改为收敛循环: 每轮只对"可下单"的票出单,全部 delta 清零才算收敛;
# 在途单/无价票跳过等下一轮 —— diff 语义天然幂等,重入安全。


def plan_orders(targets: dict, holdings: dict, blocked: set) -> tuple:
    """→ (orders, converged)

    targets : {ticker: int 目标股数}(不含 0)
    holdings: {ticker: int 当前股数}(含所有已投资票)
    blocked : 本轮不可下单的票(有在途单 / 尚无行情价)—— 跳过不出单

    orders  : [(ticker, delta, kind)],kind ∈ {"flatten","adjust"}
    converged: 所有票 delta==0 且无 blocked 残留 → True(此时才可标记版本)
    """
    orders = []
    converged = True
    for t, have in holdings.items():
        if t in targets or have == 0:
            continue
        if t in blocked:
            converged = False
            continue
        orders.append((t, -have, "flatten"))
        converged = False                        # 出单本轮必未收敛(等成交)
    for t, want in targets.items():
        have = int(holdings.get(t, 0))
        delta = int(want) - have
        if delta == 0:
            continue
        if t in blocked:
            converged = False
            continue
        orders.append((t, delta, "adjust"))
        converged = False
    return orders, converged
