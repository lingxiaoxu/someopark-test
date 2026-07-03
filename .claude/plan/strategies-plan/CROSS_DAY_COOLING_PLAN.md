# Cross-Day Cooling Period — Complete Implementation Plan

> Created: 2026-06-29  |  Updated: 2026-07-03 (v6 — **已实施**：Changes 1-4 全部落地，
> 前置 MONITOR Step 1-3 同日完成；验收全过——日历 6 用例、元数据+None 保留+幂等、
> 判定 6 场景（含 HAS/YUM 7/1→7/2 拦截）、E2E 沙盒回归。)
> （v5 历史：交叉审计 MONITOR_INTEGRITY_FIX_PLAN 后：前提修正 + 实施顺序依赖 + signal_ts 滞后修正）

## ⚠️ 0. 与 MONITOR_INTEGRITY_FIX_PLAN 的依赖关系（v5 新增，实施前必读）

交叉核对原始输出文件后确认（非转述）：

**0.1 本 plan 的动机案例大部分是假平仓（monitor 缺陷 2 的产物）**

MONITOR plan 附录 A 的 55 笔假平仓清单包含 HAS/PFE 全部 4 笔平仓。已用原始
daily_report position_monitor 记录逐笔验证：三笔 long 平仓时 z = −1.255 / −0.679 / −0.941，
exit 规则（long 平仓 iff z > −exit_z = 0）**全部不成立**，note 却写 "passed exit threshold 0.0"
——自相矛盾，是"模拟空仓被误判为退出"的铁证。churn 循环的真实链条是：

```
假 CLOSE (缺陷2) → inventory 被错误平仓 → 模拟仍持仓 → Step 2 重开 (L706) → 次日又假 CLOSE → 循环
```

**重开不是病，是系统在自我修复被 bug 杀掉的仓位。** 本 plan 反事实表的"净省 $2,720"
主要测量的是缺陷 2 的伤害，不是缺乏冷却的伤害。缺陷 2 修好后 churn 大部分自然消失。

**0.2 cooling 在缺陷 2 修复前上线会产生复合伤害**

假亏损平仓 → cooling 锁 3 交易日 → 阻止系统重建被误杀的仓位。且 cooling 减少持仓数
→ 假平仓观测频率下降 → 污染 MONITOR plan V4 的验收指标（guard 拦截率应与历史假平仓
频率同量级递减）。

**0.3 缺陷 1（T-1 数据）使 cooling 实际多冷 1 天（已验证代码）**

`extract_signals` 收到的 `signal_ts` = `historical_data.index[-1]`（DailySignal.py L1287-1288
`effective_signal_ts = last_data_ts`），而 loader 边界 bug（PortfolioMRPTRun.py:696
`end_ms` 不含 end 当天 04:00 UTC bar）使最后一根 bar 永远是 T-1。但 `last_close_date`
写的是**名义** signal_date（L1792 `date_str` / L2429 `end_date_str`）。两个时钟错位 1 天：

- 名义 6/26 检查时 signal_ts=6/25 → td(6/24, 6/25)=0 < 1 → **误拦**
- 实证：HAS/YUM 6/26 重开（真实退出后的真实重入，赚 +$4,128）会被误拦

盈利冷却实际变 2 天、亏损变 4 天。**修法二选一：**
(a) 在缺陷 1 修复后再上线本 plan（signal_ts 自动等于名义日，Change 4 原样正确）；
(b) 若先上线：Change 4 不用 signal_ts，改为把名义 signal_date 传入 extract_signals
（加可选参数 `nominal_date`，_run_single L2395 传 `signal_date`）。

**0.4 回测边恰恰靠快速重入赚钱——修完 monitor 后需重估 cooling 力度**

回测无缺陷 2（模拟正常带仓运行）。WF 记录：HAS/PFE `low_vol_specialist` OOS +$15.4k /
Sharpe 2.34 就是 24 个交易日开仓 8 次打出来的。live 假平仓修好后，live 平仓变真实、
行为向回测收敛；此时 3 天亏损冷却压制的正是产生 OOS 边的行为。**建议缺陷 2 修复后
用干净数据观察 2-4 周再决定 cooling 的天数参数（可能 3→2 或只保留止损冷却）。**

**0.5 无冲突/无顺序要求的部分**

- 缺陷 3（scale 双重缩放）：`last_close_pnl` 符号不受 scale 污染（scale>0），cooling
  盈亏归类不受影响，先后皆可；修复后存的金额自然变干净。
- MONITOR §7（load_positions 用 as_of）：与 Change 3 新增字段零冲突（PnLReport 只读
  `direction`）。
- 代码位置不重叠：monitor 修 (b) 在 `_build_signal` L627 分支内，cooling Change 4 在
  `extract_signals` L1056；可独立提交。

**0.6 本 plan 的 Deferred A/B 会加重缺陷 2，必须排在 monitor 修复之后**

MONITOR plan L97-98 明确：回测 `cooling_off` 挡住模拟重入是假平仓的触发器之一。
把回测 cooling_off 从日历日改成交易日（= 变长）会让 monitor 迷你模拟更频繁地
"空仓 + inventory 有仓" → 更多假 CLOSE。

**结论 — 推荐实施顺序：**
```
1. MONITOR Step 1 (缺陷3, 纯算术)      → cooling 元数据金额干净
2. MONITOR Step 2 (缺陷1, T-1)         → signal_ts == 名义日, Change 4 原样正确
3. MONITOR Step 3 (缺陷2, 假平仓 guard) → churn 主因消失
4. 观察 2-4 周干净数据                  → 重估 cooling 天数
5. 本 plan Changes 1-4                  → 防御真实的快速重入 (HAS/YUM 型)
6. 本 plan Deferred A/B (随下次 WF 重跑)
```
若业务上要求先上 cooling：必须采用 0.3(b) 名义日传参，并接受 0.2 的指标污染。

## 1. Problem

HAS/PFE traded 6 times in live (4 losses, net −$3,134), with same-day close→reopen on 5/22 and 6/10.
No cross-day cooling exists in live trading. 审计确认三个事实：

1. **同日重开是真实机制**：monitor (Step 1) 早晨平仓 → Step 2 同一次 run 的模拟仍持仓 → `_build_signal` L706/L717 `in_long and not inv_direction` → 再发 OPEN。6/10 HAS/PFE（盈利平仓 +590 后同日重开 → 次日 −1,884）、6/24 GLW/FOX (MTFS) 均为此模式。
2. **快速重入是被选参数的固有行为**：回测中 HAS/PFE `low_vol_specialist` 在 24 个交易日内开仓 8 次（多次 1 天往返），in-sample n_trades 高达 90-132。这不是 live 独有 bug，而是 z 徘徊在低阈值（entry_z=0.5）附近的必然结果。
3. HAS/PFE 已于 6/11 被 walk-forward 淘汰、6/18 起彻底退出 universe——本方案是针对**下一个**此类配对的预防措施。

## 2. Cooling Rules

| Close Type | Cooling (trading days) |
|-----------|----------------------|
| 确认盈利平仓（action=CLOSE 且 pnl 已知 且 pnl >= 0） | 1 |
| 其他一切情况（亏损 / CLOSE_STOP / pnl 未知） | 3 |

**pnl 未知按亏损处理**（用户规则："盈利平仓可入…别的情况至少3天"）。审计发现历史上有 22 个 CLOSE/CLOSE_STOP 事件缺 `unrealized_pnl`（`_compute_close_upnl` L603-604 在开仓价缺失或当日价 NaN 时返回 None），其中包括多个止损（RSG/AFL、CSX/AIG、ETR/AVB）——若按 `or 0` 归为盈利只冷却 1 天，会漏放止损重入。

MRPT 和 MTFS 使用相同规则（Change 4 插入在共享循环中，两策略自动同步受限）。

## 3. Scope Summary

**本次实施（仅 DailySignal.py，4 处，全为新增）：**

| 改动点 | 位置 |
|--------|------|
| Change 1: `trading_days_between()` | ~L131 (prev_weekday 之后) |
| Change 2: 冷却常量 | ~L109 |
| Change 3: 保留平仓元数据 | L1236-1237 |
| Change 4: 冷却检查 | ~L1056 (anti-churn 之后) |

**不改：** `PortfolioClasses.py`、`PortfolioMRPTRun.py`/`PortfolioMTFSRun.py`、`Portfolio*StrategyRuns.py`、`churn_blocked_pairs`、`PnLReport.py`/`RiskManager.py`/`UpdateStrategyPerformance.py`

**推迟（见文末 Deferred）：** 回测 `re_evaluate_pair` 日历日→交易日

---

## Change 1: `trading_days_between()`

**File:** `DailySignal.py` ~L131（`prev_weekday()` 之后、Inventory helpers 之前）

```python
def trading_days_between(d1, d2) -> int:
    """Count NYSE trading days strictly between d1 and d2 (exclusive both ends).
    Returns 0 if d2 <= d1 or same/adjacent trading day."""
    try:
        import pandas_market_calendars as mcal
        t1 = pd.Timestamp(d1)
        t2 = pd.Timestamp(d2)
        if t2 <= t1:
            return 0
        nyse = mcal.get_calendar('NYSE')
        start = (t1 + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
        end   = (t2 - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
        if start > end:
            return 0
        valid = nyse.valid_days(start, end)
        return len(valid)
    except Exception:
        count = 0
        d = pd.Timestamp(d1) + pd.Timedelta(days=1)
        end = pd.Timestamp(d2)
        while d < end:
            if d.weekday() < 5:
                count += 1
            d += pd.Timedelta(days=1)
        return count
```

纯新增；`pd` L92 已 import，`mcal` 局部 import 与 `prev_weekday` 同风格。

**测试预期（已核对 2026 年真实日历：6/1 周一；Memorial Day 5/25；Juneteenth 6/19 周五）：**

| d1 (平仓日) | d2 (信号日) | 结果 | 说明 |
|-------------|------------|------|------|
| 2026-06-24 (Wed) | 2026-06-25 (Thu) | 0 | 相邻交易日 |
| 2026-06-24 (Wed) | 2026-06-26 (Fri) | 1 | 中间 1 个交易日 (6/25) |
| 2026-06-29 (Mon) | 2026-06-30 (Tue) | 0 | HAS/YUM 实例：6/30 重开会被拦 |
| 2026-06-11 (Thu) | 2026-06-16 (Tue) | 2 | 6/12 Fri + 6/15 Mon（跳周末）|
| 2026-06-18 (Thu) | 2026-06-22 (Mon) | 0 | 6/19 Juneteenth + 周末全跳过 |
| 2026-05-22 (Fri) | 2026-05-26 (Tue) | 0 | 周末 + Memorial Day 5/25 全跳过 |

---

## Change 2: 冷却常量

**File:** `DailySignal.py` ~L109（`BACKTEST_BASE_CAPITAL` 之后）

```python
_COOLING_PROFIT_DAYS = 1    # trading days after confirmed-profit CLOSE
_COOLING_LOSS_DAYS   = 3    # trading days after loss / CLOSE_STOP / unknown-pnl close
```

---

## Change 3: 保留平仓元数据

**File:** `DailySignal.py` L1236-1237 — `update_inventory_from_signals()` CLOSE 分支

**Before:**
```python
        elif action in ('CLOSE', 'CLOSE_STOP'):
            inv['pairs'][pair] = {'direction': None}
```

**After:**
```python
        elif action in ('CLOSE', 'CLOSE_STOP'):
            _prev = inv['pairs'].get(pair, {})
            inv['pairs'][pair] = {
                'direction': None,
                'last_close_date': signal_date,
                'last_close_pnl': sig.get('unrealized_pnl'),   # 保留 None，不 or 0
                'last_close_action': action,
                'last_close_param_set': _prev.get('param_set'),
            }
```

**v4 修正：** `last_close_pnl` 存原始值（可为 None），**不再 `or 0`** — None 代表"未知"，由 Change 4 保守归类为 3 天冷却。若写成 0 会被归为盈利只冷 1 天，止损（CLOSE_STOP 本质是亏损）会漏放。

**三条平仓写入路径全部经过此分支（已逐条验证）：**

| 路径 | 调用点 | signal_date 类型 | pnl 是否可能为 None |
|------|--------|------------------|---------------------|
| Step 1 monitor CLOSE | L1791 (`close_sigs` 过滤后) | `date_str` 字符串 | 否（monitor 总算 pnl）|
| Step 2 信号 CLOSE（含 orphan close，如 6/24 GLW/FOX：monitor 说 HOLD 但模拟触发止损）| L2428 (完整 signals) | `end_date_str` 字符串 | **是**（L603-604 开仓价缺失/价格 NaN）|
| 单策略模式 | 同 L2428 路径 | 同上 | 是 |

**下游兼容（逐文件验证过）：** PnLReport (6处)、RiskManager (2处)、UpdateSP (2处)、DailySignal 自身 (7处) 全部用 `.get('direction')` 判断持仓，额外字段零影响。

**幂等性：** 同日重跑时 monitor 跳过已平仓对（L1726 `if not direction: continue`），Step-2 的 CLOSE 分支要求 `inv_direction` 为真（L614/L627）→ 不会二次处理、不会覆盖首次写入的元数据。

---

## Change 4: 冷却检查

**File:** `DailySignal.py` — anti-churn block (L1046-1055) 之后、Fix 6A (L1057) 之前

```python
        # Cross-day cooling: block re-entry after a close.
        # Confirmed-profit CLOSE: 1 trading day.  Loss / CLOSE_STOP / unknown pnl: 3.
        if sig.get('action') in ('OPEN_LONG', 'OPEN_SHORT'):
            _inv_pair = inventory.get('pairs', {}).get(pair_key, {})
            _lcd = _inv_pair.get('last_close_date')
            if _lcd:
                _sig_date_str = (signal_ts.date().isoformat()
                                 if hasattr(signal_ts, 'date') else str(signal_ts))
                _td = trading_days_between(_lcd, _sig_date_str)
                _lcd_pnl = _inv_pair.get('last_close_pnl')
                _lcd_act = _inv_pair.get('last_close_action')
                _is_profit = (_lcd_act == 'CLOSE'
                              and _lcd_pnl is not None and _lcd_pnl >= 0)
                _cool = _COOLING_PROFIT_DAYS if _is_profit else _COOLING_LOSS_DAYS
                if _td < _cool:
                    original_action = sig['action']
                    sig['action'] = 'MACRO_VETO'
                    sig['original_action'] = original_action
                    _pnl_str = f"${_lcd_pnl:+,.0f}" if _lcd_pnl is not None else "unknown"
                    sig['note'] = (
                        f"Cross-day cooling — {pair_key} closed {_td} trading day(s) ago "
                        f"({_lcd_act}, pnl={_pnl_str}, need {_cool}td)"
                    )
                    log.info(f"[COOLING] {pair_key}: vetoed {original_action} — "
                             f"closed {_lcd} ({_td}td ago, need {_cool}td)")
```

**v4 修正（相对 v3）：**
1. 盈利判定改为 `action == 'CLOSE' and pnl is not None and pnl >= 0`——CLOSE_STOP 一律按 3 天（止损即亏损，与 pnl 字段是否缺失无关）。
2. note 字符串对 `pnl=None` 加保护（v3 的 `f"${_lcd_pnl:+,.0f}"` 遇 None 会抛 TypeError → extract_signals 无 try/except 会炸整个 Step 2）。

**安全性（对照代码验证）：**
- 与现有 7 个 veto block 同模式；上游已 veto 的信号 action 非 OPEN_* → 不二次触发。
- `inventory`/`signal_ts`/`pair_key` 均在作用域内（L922 签名、L962 循环）。
- 被 veto 的 MACRO_VETO 不进 `update_inventory_from_signals` 的任何分支 → 元数据保持 → 次日模拟若仍持仓会再发 OPEN → 冷却到期后自然放行。**冷却是延迟，不是取消。**

---

## 同日语义（v4 修正 — v3 描述有误）

v3 声称"cooling 管 Day T+1 起，与 anti-churn 无重叠"。**实际不是**：dual 模式下 Step 1 在 L1795 保存 inventory 后，Step 2 在 L2363 重新 load，所以**当天平仓的 last_close_date 当天就可见**，`td=0 < cool` → 同日重开也会被拦。这是特性不是缺陷：

| 场景 | anti-churn | cooling | 结果 |
|------|-----------|---------|------|
| 同日**亏损**平仓 → 同日重开 | ✅ 先拦（链条在前，L1046）| 不再触发（action 已是 MACRO_VETO）| 拦住，log 记 ANTI_CHURN |
| 同日**盈利**平仓 → 同日重开 | ❌ 不覆盖（只管亏损）| ✅ 拦（td=0 < 1）| **cooling 补上了 anti-churn 的洞** |
| 次日起 | 不适用（in-memory，仅当次 run）| ✅ | cooling 独管 |

**实证：** 6/10 HAS/PFE monitor 盈利平仓 +$590（monitored:true），同次 run Step 2 重发 OPEN_LONG，anti-churn 放行（非亏损），次日亏 −$1,884。有 cooling 则该重开被同日拦截，−$1,884 不会发生。

Anti-churn 保持不动：亏损同日场景两机制重叠但链条顺序保证只 veto 一次；删除 anti-churn 也可行但不在本次范围。

---

## Test Scenarios（日期已按 2026 真实日历勘误）

**A — HAS/YUM 盈利平仓 +$4,396 于 6/24 (Wed):**
6/24 同日重开 td=0 <1 拦；6/25 (Thu) td=0 拦；6/26 (Fri) td=1 ≥1 放行。实际 6/26 开仓 → 不受影响 ✅

**B — HAS/YUM 盈利平仓 +$4,128 于 6/29 (Mon)，6/30 实际开了 LONG（方向翻转）:**
td(6/29, 6/30)=0 < 1 → **会被拦，延迟到 7/1**。冷却不区分方向——翻向重入同样受限。

> **2026-07-02 结局复盘（live 验证，从"代价样本"反转为"收益样本"）**：该 6/30 LONG
> 撞上 7/1 动量反转日（HAS 长腿跌、YUM 空腿涨），7/1 被信号退出平仓，**真实亏损
> −$6,690.20**（daily_report position_monitor 实录）。若 cooling 已上线，此仓被拦，
> 亏损完全避免；且 7/1 当天 z=-1.445 已反向穿越 exit → 冷却到期后也不会追入。
> 这是 cooling 的第一个前瞻性 live 验证案例（此前 HAS/PFE 反事实均为回溯推算，
> 且其平仓多为 monitor 缺陷 2 的假平仓，证据力弱——本例的开/平仓均为真实事件）。

**C — HAS/PFE 亏损平仓 −$1,642 于 6/8 (Mon):**
6/9 td=0、6/10 td=1、6/11 td=2 全拦；6/12 (Fri) td=3 放行。实际历史 6/9 开仓 → 会被拦。

**D — 旧 inventory `{'direction': None}` 无 last_close_date:** 跳过检查，放行（向后兼容）。

**E — 假日：亏损平仓 6/18 (Thu)，6/19 (Fri)=Juneteenth:**
6/22 (Mon) td=0、6/23 td=1、6/24 td=2 拦；6/25 (Thu) td=3 放行。

**F — 重开覆盖元数据:** OPEN 分支 L1218 整体替换 dict → last_close_* 消失；下次 CLOSE 重新写入。正确。

**G — pnl 缺失的 CLOSE_STOP（如历史 RSG/AFL、CSX/AIG）:** `last_close_pnl=None` → `_is_profit=False` → 3 天。✅ v4 修正点。

---

## HAS/PFE 反事实（近似——被拦后若模拟仍持仓，冷却到期会重入）

| # | 开仓 | 平仓 | PnL | cooling 判定 |
|---|------|------|-----|--------------|
| 1 | 05-18 | 05-19 | +$1,647 | 放行（无前序）|
| 2 | 05-21 | 05-22 | −$418 | 放行（盈利平仓后 td=1 ≥ 1）|
| 3 | 05-22 同日 | 05-26 | −$1,426 | **拦**（亏损同日 td=0 < 3）|
| 4 | 06-05 | 06-08 | −$1,642 | 放行（td≈7 ≥ 3；且 5/27-6/4 本有 low-corr veto）|
| 5 | 06-09 | 06-10 | +$590 | **拦**（亏损后 td=0 < 3）|
| 6 | 06-10 同日 | 06-11 | −$1,884 | **拦**（即使 5 发生：盈利同日 td=0 < 1）|

无 cooling：−$3,133。有 cooling：约 −$413。**净省约 $2,720**（拦 2 亏 1 盈）。
另：6/16 有一笔 zombie 时代的追溯 CLOSE_STOP −$2,234（修正 6/11 平仓价），属数据异常非第 7 笔交易。

---

## Live vs Backtest 分歧（必须知情的架构性代价）

被选中参数的高频重入**是回测里赚钱的方式**：HAS/PFE `low_vol_specialist` 入选时 OOS +$15.4k / Sharpe 2.34，正是靠 24 天 8 次开仓打出来的；HAS/YUM `aggressive` in-sample n_trades 74-108。**live-only cooling 意味着 live 交易频率系统性低于选出该配对的 OOS 统计**——live 表现会偏离 OOS 预期（方向不定：churn 的亏损被拦是增益，churn 的盈利被拦是损失；HAS/PFE 样本里净增益）。

接受理由：live 已有 correlation gate / capacity / concentration / earnings blackout / macro gate 等 6 个 live-only 否决层，cooling 是同一架构模式的第 7 层。根治需 Deferred 部分 + WF 重跑。

---

## Deferred: 回测冷却对齐（下次 WF 重跑时一并做）

1. `PortfolioClasses.py` L2072 (MRPT) / L3189 (MTFS)：`.days` → `trading_days_between()`。
2. **诚实声明：仅做第 1 条并不能对齐 live/backtest**——回测 `re_evaluate_pair` 只在**止损**后触发（走 `stop_loss_history`），正常 exit 平仓（回测里最常见）完全没有冷却，也不区分盈亏。真正对齐需给回测加"所有平仓后按盈亏冷却"的新逻辑（动 `PortfolioClasses.py` 交易循环，risk 高），届时用 WF 重跑验证 OOS。
3. param_set 里 `cooling_off_period=1..5` 数值不改，语义届时从日历日变交易日。

---

## Files Changed Summary

| File | Changes |
|------|---------|
| `DailySignal.py` | +常量 (~L109)、+`trading_days_between()` (~L131)、CLOSE 分支写元数据 (L1236-1237)、+冷却 veto (~L1056) |

## Log Output

```
[COOLING] HAS/YUM: vetoed OPEN_LONG — closed 2026-06-29 (0td ago, need 1td)
[COOLING] HAS/PFE: vetoed OPEN_LONG — closed 2026-06-08 (0td ago, need 3td)
```
