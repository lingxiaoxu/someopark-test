"""portfolio_ledger — AISS/SSRS 真实记账层（balance sheet + trade ledger）。

Mirror MRPT/MTFS 的 PortfolioClasses 记账概念（asset_cash / liabilities /
net_equity / 每日快照），落地为 daily-signal 场景的文件账本：

  account_{strat}.json                  当前账户状态（cash/positions/equity）
  account_history/account_{strat}_YYYYMMDD.json   每日快照
  trade_ledger_{strat}.jsonl            append-only 交易/分红/费用台账

设计原则（见 .claude/plan/strategies-plan/AISS_SSRS_LEDGER_PLAN.md）：
  - 每策略独立账户，利润只归本策略（复利在 Phase 4 拨开关）
  - 价格只用各自 Polygon store，禁 yfinance
  - realized 一次定格（成交价=Polygon 交易日收盘），永不重估
  - 全程当前口径（current caliber）：历史快照记录按 splits_cache 归一，
    与回溯调整后的 store 价格同口径 —— 镜像 CorporateActions 守卫 4 的锚定原则
  - 分红：ex-date 现金入账（dividends_cache，Polygon /v3/reference/dividends）
  - 每日恒等式硬断言 Assets = Liabilities + Equity
"""
from .ledger import (STRATEGIES, Account, load_store_prices, process_day,
                     daily_update, load_ledger_rows)
