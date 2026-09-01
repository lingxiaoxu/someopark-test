# 足球模拟 × Diffusion Factor Model — Plan + 实施状态

> 目标:用 DFM(arXiv 2504.06566)的思路,把 box B 每场 10–15 次 microfootball 模拟
> "发散"成每场 1000–5000 个等效样本;覆盖的不只是比分,而是高维细粒度数据
> (11 项技术统计、比赛进程、事件流);并解决**没有模拟过的对阵**(两队从未交手、
> 但各自和别的队打过)的跨对阵生成。box B 结果只读,只作为训练语料。

> **状态(2026-07-06):S1–S6 已全部实现于 `dfm/football/`**(extract / model /
> generate / validate 四个模块),验证结果见 §7。下文保留原设计,
> 实施中被数据推翻/修正的点用 **[实施修正]** 标注。

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
| united_states_vs_belgium | 9(2026-07-06 新同步,batch 未完仍在增长) |
| **合计** | **99 sims / 9 对阵 / 18 队**(Mexico、Belgium 各出现 2 次) |

每个 sim 目录(已验证结构):
- `stats.json` — 11 项技术统计 × 双方 + 比分(possession/shots/on-target%/save%/passes/completion%/offsides/sequences/goals/xG/recovery_sec)
- `match_config.json` — 双方战术向量(directness, possession_target, press_intensity, tempo, shots_target)+ narrative_seed(含 rating gap 数值)
- `trajectory.jsonl` — 4000 帧 × {min, ball(pos/holder/team/in_flight), 22 名球员(pos/action), events[]}
- 帧内 events 为自由文本,**已核验可解析出**:`Goal Scored by - X - (Team)`、`Shot Made by`、`Corner to`、`freekick to`、`Foul against`、`offside`、`penalty to`、pass completed / Possession won(转换)等
- 动作词表:none / pass / run / sprint / tackle / throughBall

**引擎不模拟红黄牌**(6 个 sim 全文扫描 0 次 "card")→ 见 §6 诚实边界。

---

## 1. 问题到 DFM 的映射

### 1.1 DFM 理念 → 足球:保留 / 改造 / 失效 对照 **[2026-07-06 重读两篇论文后补]**

DFM(arXiv 2504.06566)是金融方法,搬到足球是**借理念不是搬公式**。逐条对账:

**保留的(这是"为什么用 DFM 思想"的全部理由)**
1. 核心洞见:小样本高维下,**非线性只在 k 维子空间学,补空间便宜处理**——
   `CondFactorScoreNet` 保留 Lemma-1 架构(可学习正交 V + 解析对角补 + 子空间 MLP);
2. OU 前向、score matching、早停 t₀=0.01、反向 SDE + Tweedie——训练/采样机制照用;
3. 评估哲学:"生成样本是否改善**分布估计**"(金融:协方差/组合;足球:W/D/L TV、
   通道 W1、比分线覆盖)。

**必须改造的(金融假设在足球不成立,直接搬会错)**
1. **i.i.d. 单一分布 → 条件分布混合**:金融是同一分布抽 n 天;足球 99 个样本来自
   9 个不同对阵。DFM 根本没有条件化部件——c 向量(战术+强度)是我们加的,
   没有它模型只能学"平均比赛"。
2. **无约束连续空间 → 有界比率+小计数+硬约束**:收益随便取值;足球有
   控球和=1(logit 后精确共线!)、射正≤射门、进球≤射正。处理 = 变换层
   (logit/log1p)+ 生成后解析投影。论文不需要这层。
3. **对角特异噪声假设失效**:金融的 ε 对角近独立是几十年验证的;足球段张量的
   残差是结构化的(邻段自相关、同段射门↔进球耦合、控球两侧共线)。
   实践里 PCA 把约束方向吸进因子、投影层兜底,**但 Lemma-1 在足球空间不是定理,
   只是归纳偏置**——这就是为什么要做 §1.2 的消融来实证它有没有贡献。
4. **理论界不迁移**:Õ(d^{5/2}n^{−2/(k+5)}) 在 n=99 没有实际含义。有效性只能靠
   LOFO 闸门实证,不能拿论文定理背书。
5. **下游对齐真实世界**(用户指出):金融的"真实"是 held-out 收益;足球的"真实"
   有两层——引擎分布(模型必须忠实,否则谈不上放大)和**真实足球**
   (SFAS 论文 Table 1 区间 + 真实世界杯牌率)。二者冲突时:分布形状忠实引擎,
   已知引擎失真(牌率 12×)在**事件层**校准,归入 V5 闸门监督。

**消融实证(factor 架构 vs 无结构条件 MLP,同数据同训练)**:结果见 §7 验证表。

### 1.2 原映射表

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
  12. 场地倾斜度 **[实施修正]** 原设计为"球均 y 坐标"——实测发现引擎**半场换边**
      (双方射门 y 均双峰分布 q25≈180/q75≈900),raw y 方向不可辨识;
      已改为 |球均 y − 中线| 归一化(进攻纵深,标签中性,翻转增广安全)

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

**[实施修正]** 引擎 rating 文本是 LLM 自由发挥("Cape Verde (80) vs Argentina (63)"、
Z-score、或干脆没有),**不可稳定解析**——只有 10/99 个 sim 匹配统一格式。
已改用 §上述第三条:`extract.py` 里的静态 `TEAM_STRENGTH` 表(18 队 z-score,
可扩展,零泄漏,未模拟对阵也有定义)。战术向量(10 维)仍逐 sim 取自
match_config.json;老 sim 缺 `shots_target` 用语料中位数 12 兜底。
零 sim 对阵的战术向量由 ridge 回归 (strength_h, strength_a) → tactics 从语料拟合生成
(generate.py 内置)。δ_team 残差 embedding 未启用——LOFO 验证表明纯协变量已过闸门,
图变密后再考虑。

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

1. **红黄牌 [已反转 2026-07-06,用户是对的]**:读了 box B 引擎完整代码后确认,
   引擎**有完整牌机制**(`engine/lib/playerMovement.js:165-215`:铲球犯规
   intensity 65-90 黄 / 85-100 直红,tackle 犯规 75-90 黄 / 90-100 红,两黄自动
   变红,红牌球员 pos 锁死 `['NP','NP']` 罚下)。之前输出里看不到是因为
   **发牌处从不 `iterationLog.push`**,而 `brain/trajectory.py:49` 的帧 events
   只抄 iterationLog;球员级 `stats.cards` 也没人导出 → 牌静默发生。
   **红牌可从现有轨迹恢复**:首个 pos 变 'NP' 的帧 = 罚下时刻(引擎写 NP 的
   4 处全部紧跟 `stats.cards.red++`,受伤不写 NP)。`extract.py` 已实现:
   **99 场恢复 125 张红牌**,新增第 13 通道 `red_cards`,事件层同步输出 red_card。
   仍不可恢复的:没变成第二黄的单黄牌(零痕迹)。
   **引擎侧建议(box B 空闲后,1 行/处 ×4)**:在 4 个发牌点补
   `matchDetails.iterationLog.push(...)`,黄牌即刻可见,extract 的 card 正则自动接住。
   **引擎失真已量化并在事件层校准 [2026-07-06]**:引擎红牌 1.26 张/场,
   真实基准(卡塔尔 2022:64 场 214 黄 4 红;本届 93 场完赛,查 prediction_market
   wc.db fixture_event:238 黄 13 红)。**黄牌锚定本届实测 2.56/场**(卡塔尔混合
   2.88 虚高,用户要求 <2.6,2026-07-07 调整);红牌保留两届混合 0.108/场
   (单届红牌样本太薄)
   ——引擎红牌率 ~12 倍失真。处理(分层,不污染模型):
   模型/验证层保持引擎忠实(学引擎分布);`generate.py` 事件实现层默认
   `realistic_cards=True`:红牌强度按 REAL/corpus 缩放(形状来自模型:谁的比赛、
   哪个时段;水平来自真实),黄牌(引擎不可观测)按真实率做泊松 overlay、
   按犯规份额(对手赢得任意球的比例)分段分配。实测生成 1000 场:
   黄 2.52 / 红 0.099,对齐基准(黄 <2.6 达标)。`--engine-cards` 可切回引擎忠实模式。
   引擎本体的 intensity 窗口修正仍应并入引擎调整 plan(P 系列)。
2. **发散 ≠ 无中生有**:生成样本的信息上限就是 90 个 sims + 协变量。
   5000 个样本降低的是**蒙特卡洛方差**(比分分布、事件强度的估计噪声),
   修不了引擎本身的系统偏差(比如已发现的弱队进球压低 —— 那要靠引擎调整
   plan P1–P4,两件事正交,不要指望 DFM 替引擎还债)。
3. **零 sim 对阵的生成是外推**,置信度必须降级标注(前端如果展示,
   标"模型外推,无本对阵模拟支持")。
4. n=90 下 k 必须小(4–8),条件网络必须小(MLP 2–3 层),早停 t₀=0.01 照抄论文。

---

## 7. 验证方案(计划内置,不做完不算数)

> **[验证结果 2026-07-06,含 red_cards 的 13 通道语料]**(`football/validate.py`,
> 9 对阵 LOFO × 1000 样本,完整数字在 `football/data/validation.json`):
>
> | 闸门 | 结果 | 判定 |
> |---|---|---|
> | V1 LOFO — W/D/L TV 距离赢 pooled baseline | **5/9 对阵** | ✅ 过半 |
> | V1 LOFO — 全通道 W1 距离(中位数)赢 pooled baseline | **6/9 对阵** | ✅ 过半 |
> | V2 引导保真 — 关键统计均值落 90% bootstrap CI | 8/10、9/10 | ✅ |
> | V2 引导保真 — 覆盖留出 sims 实际比分线 | 100%、80% | ✅ |
> | V3 一致性(射正≤射门、进球≤射正、控球和=1) | 全部通过 | ✅(解析保证) |
>
> TV 的"输"里 us_belgium(0.144 vs 0.089)是该对阵 W/D/L 恰好贴近语料平均、
> pooled baseline 几乎不可战胜的情形;真正的 miss 是 cote_divoire_vs_norway
> 和 belgium_senegal(边缘)。
> **结论:跨对阵迁移闸门通过,零 sim 对阵生成可用(带外推降级标注);
> 引导模式(有 sims 的对阵)保真度达标。**
>
> **[V5 真实区间闸门(SFAS Table 1),2026-07-06]** 六项全 PASS,且生成的
> 入区间率一律 ≥ 语料自身(如 possession 100% vs 91.9%、shots 89.5% vs 72.2%)
> ——放大器平滑了引擎的极端场,没有引入区间外漂移:
> possession 30-70 ✅ / shots 5-20 ✅ / 射正% 25-50 ✅ / 传成% 50-90 ✅ /
> offsides 0-5 ✅ / goals 0-7 ✅。
>
> **[消融:DFM 因子架构 vs 无结构条件 MLP(同数据/训练/验证),2026-07-06]**
>
> | 架构 | LOFO TV 赢 | LOFO W1 赢 | W1 逐对阵对比 |
> |---|---|---|---|
> | **factor(Lemma-1 结构)** | **6/9 ✅** | **6/9 ✅** | 9 个对阵中 7 个更优 |
> | plain MLP(消融) | 3/9 ❌ 不过闸门 | 5/9 | — |
>
> **这是"借 DFM 理念是否值得"的直接实证:低维子空间学非线性 + 解析补的
> 归纳偏置,在 n=99 的足球语料上把迁移闸门从不过变成过。** 不是形式上致敬
> 金融论文——结构真的在小样本下扛了统计效率。
>
> **[细化迭代 2026-07-06 晚:四项改进 + 自噪声地板 + 一次被证伪的尝试]**
>
> 改进(全部已固化进代码):① 条件向量剔除外部静态强度表、改用引擎内生
> 战术 10 维 + 5 个战术差(发现静态表与引擎自身 rating 矛盾:引擎叙述
> Belgium≈Senegal、个别 sim Cape Verde>Argentina——喂错条件正是 miss 主因之一);
> ② 生成时逐样本从该对阵 config 池抽条件(中位数塌缩了引擎跨 sim 的配置多样性);
> ③ W/D/L 改解析 Skellam 混合(消泊松实现噪声);④ 3-seed 集成 + k=8;
> ⑤ 弱回归引导(ridge c→z 均值,方差地板保护)。
> 效果:全对阵平均 TV 0.271 → **0.227**(−16%),us_belgium 翻为 WIN。
>
> **自噪声地板**(留出 sims 自身对半拆的距离,n=10-15 的评估分辨率极限;
> 已固化进 validate.py 输出):**TV 上 6/9 对阵的模型距离低于地板本身,
> 7/9 落在 plan 规定的 1.5×地板内**。剩余两个超标:mexico_england(1.86×)、
> belgium_senegal(1.62×)。
>
> **被证伪的尝试(记录以免重走)**:原始空间条件均值强校准
> (ridge c→场均进球,再缩放 goals 通道)。LOFO 检验 ridge 本身:预测
> Argentina **−1.50** 球(实际 1.80)、Brazil 2.79(实际 0.90)——15 维 c、
> 9 个对阵支撑点,协变量→结果映射**欠识别**,强校准会放大外推误差。
> 弱引导 + 地板保护是当前样本量下的正确力度。
>
> **剩余差距的定性(诚实结论)**:favorites 胜率被 shrink(生成 Argentina 胜率
> 0.43 vs 留出 0.80)不是算法缺陷,是 8 个训练对阵下统计上诚实的后验收缩。
> 解药是**语料对阵多样性**,不是更多调参:两个超标对阵恰是训练集中缺同构型
> 样本的情形(England 客队强势型、Belgium-Senegal 均势型)。box B 每新增一个
> 对阵(尤其均势对和客强对),支撑点 +1,此表数字自动改善;到 15-20 个对阵时
> 可启用 §4 预留的 δ_team 残差 embedding。
>
> **[16 通道语料重验证 2026-07-07(+xg/sprints/sequences)]**
> LOFO:TV 5/9 对 pool(地板闸门 7/9)、**W1 9/9 全胜**(新通道显著提升全通道
> 分布匹配);V2 8-9/10、V3 全过、V5 六项全过。
>
> **[V4+V6 真实世界关联验证 2026-07-07(`real_check.py`,9 场真实淘汰赛 90' 框架)]**
> 九个模拟对阵全部是真实 WC2026 淘汰赛且 sims 先于真赛完成 → 真 out-of-sample:
>
> | 指标 | 结果 | 判定 |
> |---|---|---|
> | V4 Brier vs 真实结果 | **DFM 0.647 < uniform 0.667 < 原始 sims 0.834**(市场 0.471) | ✅ 放大器优于原始 sims,闸门过 |
> | V6b FC26 阵容评分差 vs DFM 胜负边际 | **Spearman ρ=0.967(p<0.0001)** | ✅ 与 EA 真实强度数据几乎完美单调 |
> | V6c 牌率 vs 这 9 场真实 | 黄 2.87 vs 真实 2.67 ✅;红 0.116 vs 0.222(9 场小样本,基准 0.108) | ✅ |
> | V6a 真实技术统计落生成 95% 区间 | **42.6%(n=108)——弱** | ⚠️ 见下 |
>
> V6a 的弱不是 DFM 的错而是**继承的引擎失真**(方向与两周前 sim-vs-reality 分析
> 完全一致):真实值系统性高于生成分布——角球 12/18 上溢(引擎 5.2 vs 真实 8.7/场)、
> 进球 10/18(引擎 1.95 vs 真实 2.93)、射门 7/18(21.0 vs 23.3)。DFM 忠实放大引擎,
> 引擎低进球/低角球 → 生成同样偏低。
> **处置:新增可选 real-anchor 模式**(`--real-anchor`):按锦标赛级真实/引擎强度比
> (进球×1.50、角球×1.68、射门×1.11、xG×1.50,来自 94 场真实比赛聚合,非拟合
> 这 9 场)缩放事件强度。**实测:Brier 0.647→0.587(向市场 0.471 收敛 1/3)、
> V6a 覆盖 42.6%→53.7%,Spearman 不变 0.967**——预测真实比赛时建议开
> `--real-anchor`;做引擎对拍/放大时用默认引擎忠实模式。
> 比较文件 `data/real_validation{,_anchored}.json`。引擎本体修正(P 系列)仍是治本。

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

## 8. 实施状态(2026-07-06 全部完成,代码在 `dfm/football/`)

| 步骤 | 文件 | 状态 / 要点 |
|---|---|---|
| S1 提取 | `extract.py` | ✅ 99 sims → `data/segment_tensor.npy` **(99,9,2,16)** + `conditions.json` + `events.json`;**内置 stats.json 交叉校验:1170/1188 (98.5%) 容差内**(offside 边界归属噪声 ±3 除外);possession 用 ball.holder→球员 team 解析(trajectory 里 ball.team 恒为 null,state.json tick 数与 holder 帧数精确吻合佐证);pass 口径 = 传球+传中+直塞(与引擎统计对齐);red_cards 从 NP 罚下帧恢复(125 张/99 场);**[2026-07-07 补齐] xg(按段射门份额分摊全场 xG)、sprints(冲刺动作密度)、sequences(控球段发起数,对 stats.json 校验)三通道 → 引擎 11 项统计全部原生或可导出覆盖(save% = 1−对手进球/对手射正,recovery ≈ 对手控球时间/夺回数,均可导)** |
| S2 表示/增广 | `model.py` | ✅ log1p 计数 / logit 比率;主客翻转增广 n→198;c = 战术 10 + 强度 2 + 强度差 1 |
| S3 条件网络 | `model.py` `CondFactorScoreNet` | ✅ 论文 Lemma-1 架构(可学习正交 V + 解析补),c 拼入子空间网 g_ζ;V/σ² PCA 温启动;h-加权 denoising loss(母仓库无权重 MSE 在 t→t0 爆炸) |
| S4 引导采样 | `generate.py` | ✅ 反向 SDE + 高斯矩匹配引导(解析 score,权重随 α_t 退火)+ Tweedie 终步去噪;零 sim 对阵:强度表 + ridge(strength→tactics) 构造 c |
| S5 事件层 | `generate.py` `realize_events` | ✅ 段级强度 → 泊松 thinning 分钟级事件(goal/corner/freekick/offside/**red_card/yellow_card/penalty**);点球 = freekick 子强度按语料 penalty/freekick 比转换(plan §5 兑现);射手 = 语料 per-team 进球分布(Laplace 平滑);**牌率默认校准到真实基准(黄 2.56[本届实测,生成 2.52<2.6]/红 0.108 每场,`--engine-cards` 切回引擎率)** |

### S5.1 角球 granular 处理(2026-07-09,与盘中角球信号对齐)

**角球是一等公民、和其他计数统计走同一条路**:extract 逐段解析 `Corner to - X` 事件进 `corners` 通道 → model.py log1p 后**联合扩散**(学到角球与进球/控球等相关)→ generate 段级强度泊松实现分钟级角球事件 → `--real-anchor` 均值 ×1.68(真实 8.72/引擎 5.13)。

**离散度(关键实测结论)**:真实世界杯全场角球 var/mean≈1.46。**DFM 扩散的逐样本 λ 变化本身就是复合泊松 = 天然过离散,实测 var/mean≈1.59,已达标**。加负二项 Gamma 乘子会**过冲到 3.15(重复计入)**——故 `corner_extra_dispersion` 默认 **OFF**,仅留作未来引擎收紧 sims 时补用。这修正了盘中信号用显式 NegBin 的思路:DFM 侧不需要,因为扩散已提供。

**状态耦合(显式,默认 ON)**:`corner_realism=True` 按每场累计比分 gap 缩放角球 λ(一球差 ×1.10、大比分 ×0.92,取自角球事件研究,非拟合),与 `model/inplay_corners` 一致。实测把 var/mean 从 1.59 抬到 1.83(轻微,可接受)。

### S5.3 全统计通道审计(2026-07-09)——所有 stats.json 字段现已全部扩散

用户要求"角球之外的 14 项也非常详细扩散"。审计发现 **save_pct(扑救率)、recovery_sec(回收球时间)从建库起就没进通道**——引擎 11 项统计线里唯二漏掉的。已补:
- **save_pct**:match-level 率,值复制到各段,logit 扩散(进 RATE_CHANNELS);null(门将 0 射正)兜底 0.70。因子结构现能学 save%↔失球耦合。
- **recovery_sec**:match-level 正连续量(5-60s),log1p 扩散但按 **level**(段均值)重建(新 `LEVEL_LOG_CHANNELS`,production/validate/summarize 均按 mean 不 sum);压迫代理,低回收↔高压↔多夺回/角球。
- 两者都进 extract 交叉校验(段均值 vs stats.json,容差 0.5)。**通道 17→19,旧语料重取 105 sims 交叉校验 98.8% 零 skip**;生成重建 save∈[0,1]、recovery 9-32s 合理;19 通道 LOFO/V2/V3/V5 无崩。

**当前全 11+ 统计覆盖**(new-format 15 字段全含):possession→poss_share、shots、shots_on_target%→shots_on_target、**save%→save_pct(新)**、pass_comp%→pass_comp_rate、passes、offsides、sequences、goals(real-anchor)、xg(real-anchor)、**recovery→recovery_sec(新)**、corners(anchor+耦合)、freekicks、yellow_cards、red_cards;另 trajectory 独有 turnovers_won/tackles/field_tilt/sprints。**每项都是联合扩散的一等通道**(count→log1p、rate→logit、level→log1p-mean),非外挂。

### S5.4 因子结构深挖(2026-07-09,F=Factor 的深入应用)

`factor_analysis.py` 拷问了因子结构(V=342×k 载荷、z=Vᵀx 因子得分、−D_t x 解析补 = 论文 Lemma-1),诚实结论:
- **谱分析**:342 维匹配向量,Kaiser k=70、80% 方差需 49 因子——**足球统计不低秩**(金融 3-5 因子主导,足球方差摊几十方向)。k=8 子空间仅捕获 37% 方差、63% 特异残差。
- **但 k-sweep LOFO(6/8/12/16)证明可迁移最优 k 就是 8-12(平台,W1 0.73),k=16 过拟合退化(0.751)**——小样本 n=198 下 Õ(d^{5/2}n^{−2/(k+5)}) 惩罚大 k,**37% 捕获是偏差-方差最优,不是 bug**;那 37% 是可迁移部分,其余 63% 是不该硬拟合的匹配特异噪声。**这正验证了论文用因子结构降样本复杂度的全部意义**。默认 k 8→**10**(平台内微调)。
- **高斯特异补失配检查**:原始计数 Fano 1.2-154(sprints 154/passes 31/角球 6.5),但 **log1p/logit 变换后 std 稳定在 O(1)(0.15-0.72)——变换已驯服方差异质**,高斯补基本成立;残留仅稀疏计数的偏度(goals 2.9/red 3.8),影响最小。
- **因子语义**:学到因子均 match-level(两队同号 co-load=共同强度,合理),但由高方差通道(recovery/save%/sequences/sprints)主导、是混合体。
- **验证性因子块实证 + 优化(2026-07-09,`arch='cfa'/'cfa_hybrid'`,build_cfa_beta)**:强加 6 个足球语义块(tempo/dominance/finishing/physicality/press/control,off-block 惩罚)。**LOFO 优化全程**:
  - 纯 CFA(6 硬块):W1 **0.841**(3/4)
  - hybrid(6 语义 + 4 自由残差因子):**0.822**
  - hybrid + 惩罚扫描(0.05→0.002):**0.794**(平台,default cfa_penalty=0.002)
  - 惩罚太低(0.0005):0.800(2/4,退化)
  - **探索性(默认)始终最优:0.710(4/4)**
  优化把 CFA 从 0.841 拉到 0.794(~6% 真实改善),但**在 0.79 平台、不可能够到 0.710**——语义约束有**不可消除的 ~11% 迁移税**,因为足球统计协方差主方向天生横切语义组。
- **更优雅的方法优化(Procrustes/semantic containment)**:与其训练时约束(有代价),不如训练探索性(最佳迁移)后**事后旋转朝语义对齐**——旋转不改子空间 span→生成零影响、可解释性免费。`factor_analysis` 报告每个足球概念被探索性子空间包含多少(cos-sim):**press 0.81(强含)、tempo 0.50、dominance 0.49、finishing 0.37、control 0.34、physicality 0.27**。即数据自己的因子结构认同压迫/节奏是真实驱动,身体对抗(牌/犯规稀疏)不构成协方差方向。**最终:默认探索性不变,CFA/hybrid 留作可选,可解释性靠 containment 事后读取(零成本)。**
- **跨对阵迁移的数学根源**:共享子空间(所有对阵同一个 span(V))就是未见对阵可生成的原因——LOFO 已验证。

**真实 9 场 vs 重跑 5000 场对比(`compare_real.py`)发现的改进点**:①finishing gap——生成 xG 偏高但进球偏低(pctile 1.00 vs 0.33),引擎造机会不进球,应加 xG→goals 转化调整(比抬均值锚定更治本);②进球/角球锚定对淘汰赛欠量(锚按锦标赛均值含小组赛);③控球过窄(in95% 11%,单对阵方差太紧)。

### S5.2 适配新模拟格式(2026-07-09,box B 引擎已升级)

box B 法摩新 sims 的输出**更 granular**,DFM 已适配(向后兼容旧语料):
- **stats.json 新增 `corners`/`freekicks`/`yellow_cards`/`red_cards` 字段**(旧只 11 项)→ extract 现**交叉校验角球**(补上唯一缺真值的通道),`yellow_cards` 从 stats.json 权威计数按 trajectory 黄牌事件分钟分配到段(新通道,旧语料=0)。
- **trajectory 现发 `Yellow card: <球员>` 事件**(box B 发牌 log 补丁)→ RE_YELLOW/RE_RED 已加;帧无球员名故侧归属用 stats.json。
- **trajectory 事件串/帧结构未变** → 旧解析原样适配。语料升级 16→**17 通道**,旧语料重取 105 sims 交叉校验 98.4%,零 skip。
- box B 跑完法摩 15 场同步后,DFM `extract → production` 自动纳入,无需改码。
| S6 验证 | `validate.py` | ✅ V1(LOFO+自噪声地板)/V2/V3/V5;结果见 §7;`data/validation.json` |
| **V4+V6 真实关联** | `real_check.py` **[2026-07-07 新增]** | ✅ 九个模拟对阵全部是真实 WC2026 淘汰赛且已踢完(sims 先于真赛 → 真正 out-of-sample);V4 = 90 分钟框架 W/D/L Brier vs 真实结果,对照市场赔率(wc.db match_odds)/原始 sims 频率/uniform;V6a = 真实技术统计(fixture_stats: 控球/射门/射正/角球/xG/90'进球)落在生成分布的分位覆盖;V6b = EA FC26 阵容评分差(wc.db fc_player,与 box B 模拟同源数据)vs DFM 胜负边际的 Spearman 单调性;V6c = 生成牌率 vs 这 9 场真实牌率;`data/real_validation.json` |

依赖:torch / numpy —— someopark_run 全有,**零新依赖**;
单次训练 3000 步 ≈ 30s(Mac CPU),生成 1000 场 ≈ 10s。

## 8.1 生产管线(2026-07-07 定稿,`production.py`)

**固定输出目录**:`dfm/football/production_runs/`(**只有 production.py 写这里**,
测试/验证脚本一律不碰)。**命名规范**(沿用仓库 walk-forward 的 `*_YYYYMMDD_HHMMSS`
惯例,可按对阵名或时间戳双向检索,`sorted(glob)[-1]`/mtime 取最新):

```
production_runs/
  manifest_<ts>.json                # 本次快照清单:语料 sha1 指纹、每对阵源 sims 数、
                                    #   n_samples/epochs/牌率/锚定系数、文件列表
  dfm_<matchup>_<ts>.json           # 正式结果(matchup = box B games/ 目录名,
                                    #   如 brazil_vs_japan)
  dfm_<matchup>_<ts>_samples.npz    # 生成样本本体(tensors float32 + scores + 通道名)
```

每个正式结果 JSON 同时含**两个视图**(同一批样本):`engine_faithful`(引擎忠实
放大)与 `real_anchored`(锦标赛级强度锚定+牌率校准,预测真赛用)。内容:
W/D/L、top-10 比分线、16 通道全场统计的 mean/p5/p50/p95、每场牌数、示例事件流,
外加溯源字段(源 sims 数、语料 sha1、时间戳)。

**标准流程(box B 每出一批新模拟)**:
```bash
npm run sync:microfootball          # ① 同步 box B(需 box B 空闲,勿在模拟中跑)
cd dfm/football
conda run -n someopark_run bash -c "python extract.py"     # ② 重建语料(自动含新对阵)
conda run -n someopark_run bash -c "python production.py"  # ③ 新时间戳 = 新官方快照
```
**留存策略(2026-07-07 改)**:全量跑完**自动清除旧快照**——production_runs/ 永远只有
一个权威快照,零重合(`--keep-old` 可留档);单对阵补跑(`--matchup`)不触发清除,
只在现有快照上补文件。历史快照的关键数字都记录在本文档 §7/§8.3,不依赖文件留存。

**研究/调试用法**(输出不进 production_runs):
```bash
python generate.py --home Brazil --away Japan --n 2000 [--real-anchor]  # 引导发散
python generate.py --home Brazil --away France --n 2000 --no-guide      # 零 sim 外推
python validate.py                                   # V1-V5 闸门
python real_check.py [--anchor]                      # V4+V6 真实关联
```
(conda run 会吞 `--n`,统一用 `conda run -n someopark_run bash -c "python …"`)

**清理记录(2026-07-07)**:已删除全部测试期输出(`dfm/results{,_test,_test2,_test3,_synth_mlp}`、
`checkpoints/`);保留 `dfm/results_real/`(金融复现成绩单)、`dfm/data_cache/`
(Polygon 缓存,输入非输出)、`football/data/`(语料+验证报告)。

## 8.2 前端接入 + 自动化(2026-07-07 完成)

- **前端**:MicroFootball 模块每个对阵页新增 "DFM 扩散放大" 卡片(聚合面板下方):
  W/D/L 概率条 + 比分线 top6 + 控球/射门/角球/xG 的 p50 [p5–p95] 区间 + 牌/场,
  真实锚定 ↔ 引擎忠实 一键切换,底部标注快照时间戳。数据 = `public/data/dfm_index.json`
  (86KB,由最新 production manifest 摘要而成);读取器 `getWCDfm()`(lib/api.ts);
  i18n 5 语言齐;`tsc`+`vite build` 通过,已进 dist。已有 9 个对阵全部回填。
- **自动化**:`npm run sync:microfootball` 新增 Phase C——同步完 box B 后自动
  `extract.py → production.py → 重建 dfm_index.json`,新对阵零改码自动获得 DFM 快照;
  `--skip-dfm` 跳过(快速纯资产同步)。DFM 失败时保留旧 index 不污染前端。
  (注意:sync 本身仍须 box B 空闲时才能跑。)

## 8.3 O2.5/BTTS 定价回测(2026-07-07,负面结果,如实记录)

用官方快照样本对 9 场真实 90' 赛果回测 totals 定价(泊松混合解析):

| 市场 | engine Brier | anchored Brier | 掷硬币基线 | 命中 |
|---|---|---|---|---|
| O2.5 | 0.499 | 0.363 | **0.25** | 3/9 |
| BTTS | 0.477 | 0.374 | **0.25** | 3/9 |

**结论:DFM totals 定价现状不可交易**(比掷硬币差)。机理:引擎低进球偏差 ×
DFM 条件均值收缩在 totals 维度上复合——真实 6/9 场大 2.5,而锚定后模型 p(O2.5)
仍多在 0.2-0.38。锚定把语料均值拉到 2.93,但各对阵的条件均值仍被收缩在均值之下。
这与此前 BTTS 校准项目的发现(45% vs 70% 低估)同源。**修复路径按原 BTTS deferred
计划:专门的 totals 后验校准 + walk-forward 验证,或等引擎 P 系列修掉低进球后重估;
在此之前前端只展示分布,不接交易信号。**(9 场样本小,结论方向明确但幅度待更多
真实比赛复核——每轮淘汰赛后可重跑本回测。)

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

---

## 10. 三大缺口改进方案(2026-09-01,用户点题;PLAN ONLY,执行前逐项过闸)

背景:§6 的诚实边界三年后仍然成立的三条——球员层缺席、逐帧轨迹未开采、核心张量
无比分状态耦合。语料现状:22 对阵 435 场(2026-09-01 上线批),**首次跨过 §4 当年
写下的 δ_team 解锁线(15–20 对阵)**。三项按"便宜且熟"→"贵且新"排序;每项自带
LOFO 闸门(TV/W1 + 自噪声地板)与降级路径,任何一项过不了闸就按 §9 的原则如实
降级,不硬塞进生产。

### 10.1 球员层(S10:δ_team embedding → 球员通道,两阶段)

**现状**:条件向量 c 只有队级——引擎战术 + TEAM_STRENGTH 静态差。球员只存在于
trajectory 的 ball.holder 解析里,从未进模型。

**S10a — δ_team embedding(先做,语料门槛已过)**
- 机制:c 追加两个可学习的 per-team 嵌入 δ_home/δ_away(dim 2–4,22 队),与既有
  协变量并联;当年裸 embedding 不可辨识的原因是 16 队/8 对阵二部图太稀,22 对阵
  后图密度翻倍,且 covariate-anchored 结构保留(嵌入学的是协变量之外的残差个性)。
- 实现:model.py 的 CondFactorScoreNet 条件入口加 nn.Embedding,零 sim 对阵回退
  δ=0(退化为现行为,天然降级路径);~40 行。
- 闸门:LOFO TV/W1 不劣于现行 + 至少 3/9 对阵显著改善;零 sim 迁移路径回归
  (France_vs_Sweden 类新对阵留出验证)。
- 预期:favorites 收缩缓解(§4 已诊断那是小 n 诚实后验,嵌入给了"这队就是不同"
  的合法出口)。

**S10b — 球员贡献通道(后做,依赖 extract 扩展)**
- 机制:不建球员级模型(n 完全不够),而是把阵容信息**压成队级通道**:
  首发 11 人 rating 的 {mean, top3_mean, GK_rating, 离散度} ×2 队,作为 4×2 个
  **静态条件协变量**(不是时段通道——阵容 90 分钟不变,进时段张量是浪费维度)。
- 数据源:box B match_config(阵容+rating 均可解析,§0 当年已验证 LLM rating
  文本不可解析的是**赛后评分**,首发 rating 是结构化字段——执行前重验这条)。
- 闸门:同 S10a;另加 V6b 一致性(FC26 阵容评分差 vs DFM 边际的 Spearman ≥ 现行
  0.967 不许跌)。

### 10.2 空间轨迹(S11:帧→空间特征蒸馏,不做轨迹生成)

**现状**:box B trajectory.jsonl 有完整逐帧 22 人+球坐标,extract 只取统计量。
这是最大的未开采矿,但**诚实边界先立**:n=435 训练轨迹生成模型是妄念;真实世界
无空间数据(wc.db 没有),空间通道只能 engine-faithful,永远进不了 real_anchored
视图——它的价值是让**统计量之间的相关结构**更真(空间挤压→射门质量→xG 的因果链
现在是模型瞎猜的),不是输出热图。

- 机制:每时段每队蒸馏 4 个空间标量,作为**通道 20–23**:
  ① 防线高度(后卫线均值 y);② 队形紧凑度(凸包面积/协方差行列式);
  ③ 压迫强度(失球后 5 秒内对持球人平均距离——recovery_sec 的空间对偶);
  ④ 进攻宽度利用(有球时段 x 方向散布)。
- 实现:extract.py 增帧解析段(内存注意:355 场时 trajectory 全量 ~GB 级,流式
  逐场处理);通道 19→23;production/validate 的 LEVEL_LOG_CHANNELS 归类要跟
  (全部四个是 level 型,段均值不求和)。
- 成本:extract 一次性 +~10 分钟;训练维度 342→414,n=435 下 k 可能要重扫
  (预期仍 8–12,§8 的 k-sweep 方法原样复用)。
- 闸门:LOFO 上**原 19 通道的 TV/W1 不许劣化**(新通道不能拿旧保真换)+ 新通道
  自身 TV 过自噪声地板;V5 区间闸门重跑。
- 降级:若 414 维压垮 n=435,退路是空间 4 通道只进**条件向量 c**(赛前不可知?
  不可知——那就只进 cfa_hybrid 的语义块结构做正则,零维度代价)。执行时二选一,
  以 LOFO 为准。

### 10.3 比分状态耦合(S12:通道注入 → 时段自回归,两阶段)

**现状**:342 维一次性生成,时段间耦合全靠因子隐式承担;"领先方收缩、落后方
压上"这类**状态依赖动力学**模型看不见(角球事件层的 ×1.10/×0.92 是层 2 的补丁,
层 1 无感)。

**S12a — 状态通道注入(便宜,先做)**
- 机制:通道 +1:每时段开始时的**净胜球差**(home−away,截 ±2),作为生成对象的
  一部分(不是条件)——因子模型自然学到"score_diff 高的时段里 dominance 因子
  怎么变形"。生成后自洽性由现有一致性投影保证(进球通道累计 = 状态通道差分,
  加一条投影约束,§2 的一致性框架现成)。
- 实现:extract 从 goals 通道累计即得(零新解析);d 342→360;~30 行。
- 闸门:LOFO + **专项检验**:生成样本中"先失球队后 30 分钟射门增量"的分布 vs
  语料实测(状态响应是否被学到,不只是边际对)。
- 已知风险:进球是稀疏事件,360 维里 18 个状态坐标大部分时段为 0——若 LOFO 显示
  白费维度,降级为只保留 45'/90' 两个状态坐标。
- 
**S12b — 时段自回归 rollout(贵,S12a 过闸且语料 ≥30 对阵后再议)**
- 机制:生成从"一次 342 维"改为"9 步×38 维,每步条件于已实现前段+比分状态"——
  结构上正确的状态耦合,但每步误差会累积,n=435 下 9 步条件模型每步只有 435 个
  训练点,**先验偏负**;写在这里是为了防止将来有人跳过 S12a 直接上大改。
- 闸门:与 S12a 同 + 累计误差检验(第 9 段边际不得比一次性生成差)。

### 10.4 执行顺序与总闸

```
S10a δ_team(便宜、门槛已过)→ S12a 状态通道(便宜)→ S10b 球员通道
  → S11 空间蒸馏(最贵)→ [S12b 仅当语料≥30 对阵]
```
每项独立过 LOFO 闸,过一项转正一项;任何一项使 V4 真赛 Brier(现行 0.647,
anchored 0.587)或 V6b Spearman(0.967)回退即整项回滚。所有训练/验证跑在
Mac 副本上,box B 照旧只读。语料继续是第一杠杆:每加一个对阵,上面每一项的
可行域都在变大。

### 10.5 执行注意项(2026-09-01 上线实录补记)

- **TEAM_STRENGTH 补表纪律**:每次 box B 出现新队,上线日志会打 `WARNING: team
  missing from TEAM_STRENGTH`(双缺同行打印,逗号分隔——grep 时别在逗号截断)。
  09-01 批七队(Portugal/Netherlands/Croatia/Sweden/Australia/Egypt/Bosnia)按
  08-12 先例补表后全量重跑,**22 对阵 W/D/L 两位小数全部不变**——第二次实证评分
  维度敏感度极小(brain 用内部 rating 编战术)。补表是卫生要求,不是数字驱动。
- **`--matchup` 部分补跑的 manifest 陷阱(未修)**:部分跑写只含该对阵的
  manifest,而 dfm_index 构建读"最新 manifest 的 files"——部分补跑后直接重建索引
  会把 22 对阵塌成补跑的几个。当前正确做法:凡涉及条件/通道变更一律全量重跑;
  真修法(排队):production.py 在 --matchup 模式下应把新文件**合并**进上一份
  全量 manifest 再落新 ts。
