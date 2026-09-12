# Pinned-code backlog（需要 forward-method epoch 才能上线的改动）

**为什么需要 epoch**：`util/ model/ strategy/ config/ ingest/ venues/ jobs/ exec/` 八个目录
（外加 `util/forward_methods.runtime_snapshot()` 里点名的 26 个 ops/config 文件、以及
`identity_manifest_id()` 哈希的 `config/club_identity.json` + `data/priors/aliases_*.json`）
被激活 epoch 的运行时指纹钉住。改动其中任何一个字节，paper 前向路径**立刻停止交易**
（`entry_method: Running code or parameters differ`），直到登记并激活一个带新指纹的 epoch。
2026-09-11 18:05 UTC 曾因此中断生产 16 分钟。

**上线方式**：`ops/forward_epoch_update.py precheck|activate|verify`，要求无进行中比赛、
无未平 paper 仓位、三个 launchd 服务已卸载。窗口通常是 UTC 02:30–11:30（南美夜场结束
到次日首场之间）。规则不变的改动只换指纹，兼容集自动取传递闭包，旧前向入场继续参与
PIT 校准。

> 本清单是**单一真值**。新发现的 pin 改动写进这里，不要只留在对话或记忆里。
> 上线一项就从表里删除并在底部「已上线」追加一行。

---

## 待办

### 1. Kalshi 联赛名变音符误伤合法报价
- **文件**：`venues/kalshi/discovery.py`（`_milestone_schedule_binding`，约 :320）
- **现象**：Kalshi 对跨 UTC 午夜的晚场按**后一天**命名票号
  （`KXBRASILEIROGAME-26SEP13BOTRBB` vs 我们的 2026-09-12T23:30Z），日期对不上就走
  milestone 兜底校验；该校验要求 `detail['league'] == comp.name` 逐字相同，而 Kalshi 写
  `'Brasileiro Serie A'`、我们配置 `'Brasileirão Série A'` → 拒绝绑定，
  `schedule_metadata_unverified`。
- **代价**：2026-09-12 有 2 场（1492372、1492371）完全失去 Kalshi 报价。巴甲/阿甲晚场
  大量落在该时间带，属系统性。
- **修法**：联赛名比较做变音符归一化（NFKD + 去重音）或接受场馆写法为别名。
  护栏其余断言**一条都不放松**。
- **验收**：1492372 类场次 `kalshi.state == 'ok'`；构造一个联赛名不匹配且**队伍也不匹配**
  的用例仍被拒。

### 2. review 日志剥掉 `binding.identity_evidence`
- **文件**：`ops/live_refresh.py`（`_append_review_log`，约 :196-220）
- **现象**：`opportunities[].selected_quotes[].receipt.binding.identity_evidence` 嵌入场馆
  整个赛事负载（含该赛事全部市场定义），占文件 **98%**；单行从 4KB（9/10）涨到 540KB（9/12）。
- **代价**：246MB/天，9/11 磁盘满的疑似推手之一。
- **修法**：写日志时只删 `binding.identity_evidence` 这一个键，receipt 其余 31 个字段
  全部原样保留。**不可改成只存 receipt_id**——日志里 1,300 个 id 有 495 个（38%）不在
  `quote_receipt_v1`（该表只收执行路径的报价），日志是唯一副本。
- **验收**：单行回到 ~5KB；`binding_id` 仍在（它是含 identity_evidence 的哈希，可验证）；
  三个 `analysis/inplay_*.py` 输出不变。
- **上线后**：`ops/archive_review_logs.py` 的每日归档仍保留（异地备份价值不变）。

### 3. stale-minute 重试限流
- **文件**：`ops/live_refresh.py`（`_sync_live_until_fresh`，约 :492-510）
- **现象**：上游 API-Football 比赛时钟滞后 6–7 分钟时，每轮对**每场**重试 ≤3 次
  × 4 个端点，2026-09-12 全日烧 2,878 次。
- **代价**：周期从 90 秒拉到 6–7 分钟（峰值 11m22s），直接导致 4、5、6 三项的后果。
- **修法**：单场滞后不应拖累整轮——按 fixture 记录连续失败并短期退避；整轮重试预算封顶。
- **验收**：构造上游滞后场景，周期时长不随 live 场次线性增长。

### 4. PRE 暂存后同轮决策
- **文件**：`ops/live_refresh.py`（upcoming 尾段 → paper 段之间）
- **现象**：观测在周期早段写入、决策在周期尾段执行；周期一长就超 120 秒新鲜度护栏，
  盘前腿成批 `missing_fresh_observation`。
- **代价**：2026-09-12 早上三波共 11 场盘前腿丢失，之后靠 `ops/pre_fastpath.py` 兜住
  （拐杖，不是解法）。
- **修法**：`_stash_pre` 之后立即对该 fixture 决策（同一批函数，不放宽任何护栏）。
- **验收**：长周期下盘前腿仍入账；`pre_fastpath` 可退役为纯备份。

### 5. 跳过必须留评估痕迹
- **文件**：`ops/paper_trading.py`
- **现象**：`window_expired` / 当日模型首建惩罚导致的跳过**不写** `paper_evaluation_v2`。
- **代价**：缺席无据可查——正是 8 月 61 场缺口至今无法归因的原因。
- **修法**：每条跳过路径都落一行痕迹（epoch/fixture/track/milestone/verdict）。
- **验收**：制造一次 window_expired，表里有对应行。

### 6. 当日模型预建进日刷新尾部
- **文件**：`ops/refresh_all.py`（pin 名单内）或 `ingest/club_prior.py`
- **现象**：每赛事当天**第一次**决策时现场建模，`available_at` 晚于本轮 cutoff →
  `ObservedInputsUnavailable` 一轮惩罚；比赛日周期慢时重试轮到达前 PRE 窗已关。
- **代价**：2026-09-12 13:00 EPL + 13:30 意甲五连场的盘前腿全丢。
- **修法**：11:30 日刷新尾部对当天有比赛的赛事逐个 `build_observed_strength`，
  惩罚在空窗期付掉。临时替代：手工预建脚本（已验证 7/7 赛事可行）。
- **验收**：比赛日首次决策直接命中已存模型，无 penalty 日志。

### 7. `persist_model_run` 从不被调用
- **文件**：待定（`refresh_model` 写 `soccer_model.json` 的那条路径）
- **现象**：`health.json` 报 `model_freshness ALERT — no model_run recorded`。
- **代价**：模型新鲜度告警永久失真（目前靠 `model_export_freshness` 兜底）。
- **修法**：先确认是遗漏还是有意为之，再决定补登记还是改告警口径。

---

## 待查（尚未定位，可能不涉及 pin 文件）

- **demo 镜像接进 `pre_fastpath` 后仍 0 单**：2026-09-12 全天 21 条盘前腿、
  `mirror=0act` 每轮。已排除：`enabled()` 为 True；Decimal 序列化已修；
  `execution_context` 的「开球后不补 PRE 单」规则已由同拍调用规避。下一道闸未知，
  需在开赛前逐 fixture 走 `demo_forward.run` 的 continue 分支定位。

---

## 已上线

| 日期 | epoch | 内容 |
|---|---|---|
| 2026-09-12 04:29 | `66b4b708c658` v8 | demo 镜像 Decimal 序列化（venue→evidence 边界转 str） |
| 2026-09-12 04:35 | `ad13a1ccf038` v9 | evidence tiers 改为读取时从记录重算 |
| 2026-09-12 06:02 | `5965a28acf3e` v10 | 86 对 ClubElo 名桥、KF Víkingur 别名、南美杯 comp 内别名 |
