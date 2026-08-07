"""pairs_ledger — MRPT/MTFS 成交账本（镜像 qlib-main/portfolio_ledger 的设计）。

**为什么另建而不是扩展 qlib-main/portfolio_ledger**：
qlib-main 及其下所有文件是 AISS/SSRS 的生产资产，**只读、只参考、绝不修改**
（用户红线 2026-08-06）。本包在仓库根独立实现，产出与其**逐字段同构**的
account / trade_ledger / account_history，因此下游（报告、QuantConnect 播种）
可用同一套口径消费五个策略的账本。

**环境**：MRPT/MTFS 一律 `someopark_run`；AISS/SSRS 一律 `qlib_run`。
本包只被 pairs 侧使用，不进 qlib_run。

与 portfolio_ledger 的两处**有意差异**（pairs 特性所需）：
1. **带符号持仓**：pairs 的 s2 腿是融券做空（负股数）。本包的 `Account.trade`
   实现多空通用的持仓记账（同向加权平均 / 反向实现盈亏 / 支持翻向），
   而 portfolio_ledger 的实现只支持只做多（卖出超过持仓会断言失败）。
   只做多场景下两者行为等价（见 tests）。
2. **成交价来源**：pairs 用决策价（inventory 记录的 `open_s{1,2}_price` /
   平仓日的决策价），而非策略自有的 Polygon store 收盘。价格核对源为
   MongoDB `stock_data`（与 PnLReport 同源）。
"""
from .ledger import Account, INITIAL_CASH, STRATEGIES          # noqa: F401
