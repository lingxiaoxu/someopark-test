# 足球模拟 × Diffusion Factor Model — 研究方向 Plan(先研究,不写代码)

> 目标:用 DFM(arXiv 2504.06566)的思路,把 box B 每场 10–15 次 microfootball 模拟
> "发散"成每场 1000–5000 个等效样本;覆盖的不只是比分,而是高维细粒度数据
> (11 项技术统计、比赛进程、事件流);并解决**没有模拟过的对阵**(两队从未交手、
> 但各自和别的队打过)的跨对阵生成。box B 结果只读,只作为训练语料。

---

## 0. 现有语料盘点(已实地核验,2026-07-06)

本地已同步副本(只读):`someo-park-investment-management/server/sim_assets/microfootball/`

| 对阵 | sims |
|---|---|
| argentina_vs_cape_verde | 10 |
| belgium_vs_senegal | 10 |
| brazil_vs_japan | 10 |
| cote_divoire_vs_norway | 10 |
| mexico_vs_ecuador | 10 |
| mexico_vs_england | 15 |
| paraguay_vs_france | 15 |
| spain_vs_austria | 10 |
| **合计** | **90 sims / 8 对阵 / 16 队**(Mexico 出现 2 次,其余各 1 次) |

每个 sim 目录(已验证结构):
- `stats.json` — 11 项技术统计 × 双方 + 比分(possession/shots/on-target%/save%/passes/completion%/offsides/sequences/goals/xG/recovery_sec)
- `match_config.json` — 双方战术向量(directness, possession_target, press_intensity, tempo, shots_target)+ narrative_seed(含 rating gap 数值)
- `trajectory.jsonl` — 4000 帧 × {min, ball(pos/holder/team/in_flight), 22 名球员(pos/action), events[]}
- 帧内 events 为自由文本,**已核验可解析出**:`Goal Scored by - X - (Team)`、`Shot Made by`、`Corner to`、`freekick to`、`Foul against`、`offside`、`penalty to`、pass completed / Possession won(转换)等
- 动作词表:none / pass / run / sprint / tackle / throughBall

**引擎不模拟红黄牌**(6 个 sim 全文扫描 0 次 "card")→ 见 §6 诚实边界。

---

## 1. 问题到 DFM 的映射

论文的设定:n≈240 个观测、d≈100–500 维、k≪d 个因子;score 分解成
k 维非线性部分 + 解析线性补,样本复杂度指数只依赖 k 不依赖 d —— 这正是
小样本高维的救命结构。

足球这边的对应:

| 论文 | 足球 |
|---|---|
| 一天的 d 维资产收益向量 | **一场 sim 的 d 维"比赛表示向量"**(见 §2) |
| n≈240 个交易日 | n=90 个 sims(池化全部对阵) |
| k 个共同因子 | 比赛的低维生成因子:实力差、风格对冲、节奏、混乱度…(见 §4) |
| 生成合成收益 → 更好的协方差估计 | 生成合成比赛 → 更稳的 W/D/L、比分分布、统计分布、事件强度 |

n=90 比论文的 240 还小,所以**不能只靠无条件 DFM**,必须加两根拐杖:
条件化(conditioning)+ 分层生成(hierarchy)。这是本 plan 的核心设计。

---

## 2. 比赛表示向量 x ∈ R^d(diffusion 的直接对象)

**不 diffuse 4000 帧原始轨迹**(n=90 根本不够,也没必要)。把每场 sim 压成一个
分段-统计张量,展平成 x:

- 时间轴分 **T=9 段**(每段 10 分钟当量,4000 帧 ≈ 每段 444 帧)
- 每段 × 每队 **~12 个通道**(全部可从 trajectory.jsonl 逐帧事件+球权直接算出,已验证):
  1. 控球份额(poss ticks share)
  2. 传球数(log1p)
  3. 传球成功率(logit)
  4. 射门数
  5. 射正数
  6. 进球数
  7. 段内 xG(可由射门位置粗算,或按全场 xG 按射门分摊)
  8. 转换/夺回球权次数(Possession won)
  9. 角球数
  10. 任意球+被犯规数
  11. 越位数
  12. 场地倾斜度(该段球均 y 坐标,标准化)+ sprint 密度(挤同一通道或拆开)

维度:9 段 × 2 队 × 12 ≈ **216**,再拼上全场级 11×2 统计(22)与比分(2)
→ **d ≈ 240**。恰好落在论文的 d 区间;k 取 4–8。

**一致性约束的处理**(生成向量必须自洽):
- 比例类走 logit、计数类走 log1p / anscombe 变换后 diffuse,反变换回来;
- 全场统计 = 各段之和 → 生成后用**解析投影**(把全场维度直接改为段和,或训练时就
  只 diffuse 段级、全场级由求和派生 —— 推荐后者,d 降到 ~218);
- 进球等整数事件:diffusion 输出的是**段级强度 λ**,离散实现交给 §5 的第二层。

---

## 3. 三层生成架构(核心)

```
第 1 层  条件 DFM(本 plan 的主体)
        s_θ(x_t, t, c)  在 90 个 sims 上训练;c = 对阵条件向量(§4)
        → 任意对阵、任意数量地采样段级统计张量 x
第 2 层  事件实现(解析,无需学习大模型)
        段级强度 λ(进球/角球/任意球/越位) → 非齐次泊松 thinning 采出
        事件的分钟时间戳;进球归属射手 = 从该队 sim 语料的
        (球员×进球|射门) 条件分布抽样
第 3 层  (可选)叙事/轨迹装饰
        把生成的统计+事件序列喂给 box A nemo 写文字复盘;
        不生成新轨迹 —— 轨迹只属于真 sim
```

**为什么这样分层是对的**:10–15 → 1000–5000 的"发散"要发散的是
*比赛级别的联合分布*(谁赢、赢几个、过程什么形状、事件何时爆发),
不是像素级轨迹。第 1 层管联合分布形状,第 2 层保证事件粒度与整数性,
互不越界,各自都能验证。

### 每场 10–15 → 1000–5000 的具体机制

单场 10 个 sims 当然不足以独立训练;办法是**池化 + 条件化 + 引导**三合一:

1. **池化训练**:90 个 sims 全部进训练集,模型学到的是"一场像样的比赛
   在 d 维空间里长什么样"(共同流形,对应论文的因子结构);
2. **条件化**:c 向量(§4)告诉模型当前生成的是哪个对阵 → 条件均值/协方差移位;
3. **矩匹配引导(moment-matching guidance)**:对已有 10–15 个 sims 的对阵,
   反向 SDE 采样时加一项解析引导势
   `∇_x log N(Ax | m_fixture, Σ_fixture)`(A = 取关键统计的投影矩阵,
   m/Σ = 该对阵自己 sims 的经验矩)—— 相当于 classifier guidance 的
   高斯特例,无需再训练分类器。这样生成的 5000 个样本
   **中心钉在该对阵自己的 sims 上,变化形状借全语料的流形**。
   guidance 权重 = 插值旋钮:0 → 纯先验(跨对阵模式),大 → 贴紧本对阵。

---

## 4. 因子/条件设计 c —— 跨对阵迁移的解法(“factor 自己想办法”)

**关键困难先摆出来**:16 支队,除 Mexico 外每队只出现在 1 个对阵里。
自由学习的 team embedding 在这种图上**不可辨识**(队伍效应和对手效应混淆)。
所以不能用裸 embedding,要用 **covariate-anchored embedding**:

```
z_team = g(观测协变量) + δ_team(小残差, 强正则)
观测协变量(全部现成、只读可得):
  - 引擎自己的 rating(match_config narrative_seed 里就有 "Home +0.76 vs Away +0.13")
  - 战术先验:directness / possession_target / press_intensity / tempo / shots_target
    (match_config.json 每场都有,同队跨 sim 基本稳定)
  - (可选)Elo/FIFA 排名 —— 静态常数表,不碰任何生产数据
```

条件向量:`c = [z_home, z_away, z_home−z_away, rating_gap, tactics_home, tactics_away]`,
维度 ~20–30。score 网络在现有 `FactorScoreNetwork`(MLP 路径,已修好)上把 c
拼进输入即可(结构改动小)。

**为什么这能迁移**:Brazil 没打过 France,但 g(·) 把两队都映到同一协变量空间;
模型在 8 个对阵上学到的是"**rating gap × 风格对冲 → 比赛形状**"的映射,
不是"Brazil vs Japan"这个名字。生成 Brazil–France 时 c 照常构造,
第 3 步的矩匹配引导关掉(没有本对阵 sims),纯条件先验出样 —— 这正是
DFM 里"因子载荷张成的子空间外推"的足球版。

**训练技巧(免费加倍数据)**:主客翻转增广 —— 每个 sim 交换 home/away 通道
同时交换 c 里的 z_home/z_away → n 有效 ×2 = 180。(引擎本身无主场优势设定,
翻转是精确对称;若后续引擎加主场优势,则加一个 side 指示位再翻。)

---

## 5. 事件层(第 2 层)细节

- 段级强度向量 λ(9 段 × 2 队 × {goal, corner, freekick, offside, shot})直接来自
  第 1 层输出(计数通道的期望);
- 段内时间戳:均匀细分 + 泊松 thinning;进球时刻天然带"比分进程"→
  可输出**每一分钟的实时比分轨迹**,支持 in-play 模块直接消费;
- 射手分配:P(球员 | 队, 进球) 从 90 个 sims 的 `Goal Scored by` 事件统计
  (贝叶斯平滑到该队射门分布上,免得小样本射手表过尖);
- 红黄牌:引擎不产生 → **第 2 层不输出牌**(见 §6);
- 点球:语料里有 `penalty to` 事件,可并入 freekick 通道的一个子强度。

---

## 6. 诚实边界(自查:哪些做不到 / 不该做)

1. **红黄牌无法从 sim 语料学**(引擎 0 牌)。要么不输出,要么将来用真实比赛
   的牌频做一个独立 overlay —— 但那是另一个数据源、另一个 plan,不混进来。
2. **发散 ≠ 无中生有**:生成样本的信息上限就是 90 个 sims + 协变量。
   5000 个样本降低的是**蒙特卡洛方差**(比分分布、事件强度的估计噪声),
   修不了引擎本身的系统偏差(比如已发现的弱队进球压低 —— 那要靠引擎调整
   plan P1–P4,两件事正交,不要指望 DFM 替引擎还债)。
3. **零 sim 对阵的生成是外推**,置信度必须降级标注(前端如果展示,
   标"模型外推,无本对阵模拟支持")。
4. n=90 下 k 必须小(4–8),条件网络必须小(MLP 2–3 层),早停 t₀=0.01 照抄论文。

---

## 7. 验证方案(计划内置,不做完不算数)

**V1 — Leave-one-fixture-out(核心验证,直接检验跨对阵迁移)**
轮流留出 1 个对阵(优先留 mexico_vs_england,因 Mexico 另有对阵、England 没有,
两种难度都覆盖),用其余 7 个训练,零引导生成留出对阵 2000 样本,对比其真实
10–15 个 sims:
- W/D/L 三元分布 vs 留出 sims 经验分布(TV 距离;及 Brier vs 把留出 sims 当"真值")
- 每通道 Wasserstein-1 距离(生成 vs 留出)≤ "随机拆分留出 sims 自身两半"的距离 × 1.5
- 段级统计协方差相对误差:生成 5000 样本的 cov vs 留出 sims 样本 cov,
  同时报告 pooled-baseline(全语料均值)作对照 —— **必须赢 pooled baseline 才算迁移成功**

**V2 — 引导保真**(有 sims 的对阵):开引导生成 5000,关键统计的均值/方差
应落在该对阵 sims 的 bootstrap 置信区间内;比分分布覆盖 sims 出现过的所有比分。

**V3 — 一致性硬检查**:所有生成样本 on-target ≤ shots、goals ≤ on-target、
控球和=1、全场=段和、事件层分钟时间戳单调 —— 100% 通过(解析保证,测试兜底)。

**V4 — 下游 sanity**:8 个对阵的生成 W/D/L vs 市场赔率的 Brier,
不应劣于直接用 10–15 sims 频率估计的 Brier(样本平滑应该略优或持平)。

---

## 8. 实施排期(待批准后;全部新代码,建议放 `dfm/football/` 子目录)

| 步骤 | 内容 | 量级 |
|---|---|---|
| S1 | `extract.py`:trajectory.jsonl → 段级张量 + 事件表(只读 box B 副本,写入 dfm/football/data/) | 1 天,纯解析 |
| S2 | 表示/变换层 + 主客翻转增广 + c 向量构造 | 0.5 天 |
| S3 | 条件 score 网络(拼 c 进现有 FactorScoreNetwork)+ 训练循环复用 score_matching.py | 1 天 |
| S4 | 矩匹配引导采样(DiffusionSampler 加一个 guidance 回调) | 0.5 天 |
| S5 | 事件层(泊松 thinning + 射手分配) | 0.5 天 |
| S6 | V1–V4 验证脚本 + 报告 | 1 天 |

依赖:torch / numpy / sklearn —— someopark_run 里全有(本次修 dfm 已验证),
**零新依赖**;计算量:d≈220 的 MLP、n=180(增广后),Mac CPU 分钟级/box A 更快。

---

## 9. 自查结论(doable?)

- **每场 10–15 → 1000–5000**:✅ doable。池化流形 + 矩匹配引导在数学上就是
  "借全语料形状、钉本对阵中心",第 1 层是标准条件 diffusion,第 2 层是解析的。
- **高维细粒度(11 统计 + 进程 + 事件)**:✅ doable,d≈220 段级表示已验证
  全部字段可从现有 trajectory.jsonl 解析(§0 实测);红黄牌除外(§6.1,引擎不产)。
- **零 sim 对阵迁移**:⚠️ doable-with-caveat。16 队/8 对阵的二部图太稀,
  裸 embedding 不可辨识 —— 已改为 covariate-anchored(rating+战术),
  迁移走协变量通道,V1 的 LOFO 是硬闸门:**赢不了 pooled baseline 就承认
  迁移失败、只发布有 sims 对阵的发散**(降级路径预留,plan 不会全盘报废)。
  box B 每新增一个对阵,图变密,迁移自动变好。
- **n=90 训练 diffusion**:⚠️ 论文 n≈240 已属小样本,90(增广后 180)更极端。
  对策已内置:k≤8、小网络、早停、条件化分掉大部分复杂度;V1/V2 若过拟合
  (生成分布塌缩到训练 sims 附近),退路是把第 1 层降级为
  "因子高斯 + copula"(仍用同一表示与事件层,损失非线性但绝对稳)。
- **约束合规**:✅ box B 只读(用 Mac 上已同步副本)、生产目录零写入、
  新代码独立子目录、零新依赖。

**总评:核心路径 doable;两个 ⚠️ 都有硬验证闸门和显式降级路径,不会做成半吊子。**
