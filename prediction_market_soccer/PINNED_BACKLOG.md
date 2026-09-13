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

### 4. PRE 暂存后同轮决策（**已降级：先观测再决定**）
- **文件**：`ops/live_refresh.py`
- **状态**：2026-09-13 复核代码后发现 `_stash_pre`（在 `_refresh_upcoming_board` 内）与
  `_paper_and_demo` 本来就是相邻调用，中间没有别的阶段。2026-09-12 的批量
  `missing_fresh_observation` 更可能是**周期整体被第 3 项的重试循环拖垮**所致，而不是
  这两步之间的间隔。
- **下一步**：等第 3 项上线后的第一个满负荷比赛日，测量「PRE 观测写入 → run_cycle 开始」
  的真实间隔。仍 >120s 才动顺序；否则关闭本项。
- **临时兜底**：`ops/pre_fastpath.py`（非 pin）仍在，需要时手动起。

### 7. `persist_model_run` 从不被调用
- **文件**：待定（`refresh_model` 写 `soccer_model.json` 的那条路径）
- **现象**：`health.json` 报 `model_freshness ALERT — no model_run recorded`。
- **代价**：模型新鲜度告警永久失真（目前靠 `model_export_freshness` 兜底）。
- **修法**：先确认是遗漏还是有意为之，再决定补登记还是改告警口径。

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
| 2026-09-13 03:0x | `aabbce3970b8` v11 | ① Kalshi 联赛名实测别名表（12 赛事中 7 个名字不同，该校验路径对它们从未通过）<br>② review 日志剥 `binding.identity_evidence`（单行 87KB→27KB，receipt 33 字段全留）<br>③ stale-minute 仅对瞬时抖动重试（持续上游滞后改为记录不重试）<br>⑥ 当日模型预建进 `refresh_all`（7 赛事实测全建成） |

### 已证伪（无需修）
- **原第 5 项「跳过不留痕」**：2026-09-13 复核发现痕迹一直都在，写的是
  `paper_data_state_event`（577 行）而非 `paper_evaluation_v2`；2026-09-12 丢掉的 11 场
  每场都有 `missed_data/window_expired` 行。原条目是表名记错。
