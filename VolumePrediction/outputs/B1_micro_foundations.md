# B.1 微观基础推导(模块文档,plan §三 G6 L159 留档)

> 审计补齐 2026-08-09。此前推导散在 `models/econ.py` / `econ/policy.py` docstring,
> 本文汇为一处;每步给出代码实现与测试断言的交叉引用。

## 1. 微观基础:两项成本的权衡

单期、单标的。基金当前仓位 $x^0$,目标仓位 $x^*$,本期实际移动到 $x$。

**交易成本**(Kyle 线性冲击的二次型,论文 §4.1):

$$\text{TradingCost} = \tfrac{1}{2}\lambda\,(x-x^0)^2, \qquad \lambda(v)=\frac{0.2}{V}=0.2\,e^{-v}$$

其中 $V$ 为美元成交量、$v=\log V$(§7.1 口径:$V=\text{shares}\times vw$)。
系数 0.2 = PriceImpact 系数 0.1(`policy.IMPACT_COEF`)含二次型 ½ 的 2 因子
(`policy.LAMBDA_COEF`)。**流动性越差(V 越小),λ 越大,动仓越贵。**

**跟踪误差成本**(错过目标仓位的机会成本):

$$\text{TrackingError} = \tfrac{1}{2}\mu\,(x-x^*)^2$$

**μ 的微观基础**:均值-方差基金,风险厌恶 γ、波动 σ、AUM $A$,信号 $m$(预期
超额收益)下的无摩擦最优仓位为

$$x^* = \frac{A}{\gamma\sigma^2}\,m \qquad\text{(留档关系,plan L159)}$$

偏离 $x^*$ 一单位的二阶效用损失 $=\gamma\sigma^2/A$,故

$$\boxed{\;\mu=\frac{\gamma\sigma^2}{A}\;}$$

μ 是**按 AUM 调优的超参**:AUM 越大 μ 越小(同比例偏离的美元代价被 $A$ 摊薄),
这正是表 3 / fig5 中"AUM 越大、绝对收益越低、最优交易率越保守"的机制。
生产侧 μ 不用论文场景值,走 `econ/calibration.py` → `mu_calibration.json`
(alpha-decay 实测;§7.12 profiles 经 `resolve_mu` 消费,缺工件降级 paper_prior 并标注)。

## 2. 归一化:losscon

令归一化交易率 $z=\dfrac{x-x^0}{x^*-x^0}\in[0,1]$(本期完成目标缺口的比例),
把两项成本除以公因子 $\tfrac12(x^*-x^0)^2$:

$$\boxed{\;\text{losscon}(v,z;\mu)=\lambda(v)\,z^2+\mu\,(1-z)^2\;}$$

实现:`econ/policy.py::losscon`(μ=∞ 按极限语义:z<1 → ∞,风控离场绝不因成本延迟);
`models/econ.py::losscon`(torch 版,经济学习的训练损失)。

## 3. 最优策略闭式解(S 形曲线,图 2)

一阶条件 $\frac{d}{dz}[\lambda z^2+\mu(1-z)^2]=0$:

$$z^*=s(\bar v;\mu)=\frac{\mu}{\mu+\lambda(\bar v)}
=\frac{1}{1+\exp(-\bar v+\ln 0.2-\ln\mu)}$$

第二等号代入 $\lambda=0.2e^{-\bar v}$ 即得——**关于 $\bar v$ 恰是 sigmoid**
(论文图 2 的 S 形;复刻产物 `outputs/replication/fig2_s_curve.png`)。
边界:μ→∞ ⇒ z*→1(必须成交);μ→0 ⇒ z*→0(只在乎成本)。
实现:`econ/policy.py::s_opt`;逆映射 `s_inv`(fig3 损失面用)。

**测试断言**(G6 验收,`tests/test_econ_execution.py` 27 项):
闭式解与数值优化一致(1e-6);μ→∞ ⇒ z→1 收敛断言(`test_trading_experiment.py`)。

## 4. B.2 等价形式(实现为恒等式断言)

以最优交易率 $z^*$ 参数化:由 $z^*=\mu/(\mu+\lambda)$ 反解 $\lambda=\mu\frac{1-z^*}{z^*}$,

$$\text{losscon\_zz}(z^*,\hat z;\mu)=\mu\frac{1-z^*}{z^*}\hat z^2+\mu(1-\hat z)^2
\;\equiv\;\text{losscon}(v,\hat z;\mu)$$

实现:`models/econ.py::losscon_zz`;恒等式为测试断言(两种写法数值一致)。

## 5. 鲁棒性替代形式(注 16)

$\lambda=0.2\sqrt{1/V}=0.2e^{-v/2}$(`policy.FORM_SQRT`)做一组对照——
`lambda_of_v(v, form)` / `losscon(..., form)` / `s_opt(..., form)` 全链支持,
G6 验收含替代形式单测。

## 6. 与双学习范式的衔接(G7,命题 1)

- **统计学习**:min MSE 得 $\hat v$,交易 $z=s(\hat v;\mu)$(插入解)。
- **经济学习**:$z=\text{net}(X)$ 直接 min mean losscon。
- **命题 1(Bregman)**:losscon 关于 v 非 Bregman 散度 ⇒ $E[v|X]$ 的插入解
  ≢ 经济最优。实证:表 2 结构再现(经迁移路线 transfer_econ 全 7 档 μ 优于
  nn_stat,`table3_transfer.csv`;从零直训失败留档 `G7_divergence_report.md`)。
- **oracle**:$z=s(v_{true};\mu)$,MEL 归一化 100% 端;ma5 基线($\hat\eta=0$)=0% 端。

## 交叉引用总表

| 对象 | 公式 | 实现 | 验收 |
|---|---|---|---|
| λ(v) | 0.2e^{−v} / 0.2e^{−v/2} | `policy.lambda_of_v` | G6 单测 |
| losscon | λz²+μ(1−z)² | `policy.losscon` / `econ.losscon`(torch) | 27 项单测 |
| s(v̄;μ) | μ/(μ+λ) = sigmoid | `policy.s_opt` | 1e-6 闭式↔数值 |
| μ=γσ²/A | AUM 映射 | `policy.mu_from_aum` + `calibration.py` | G7 表2/表3 |
| x*=(A/γσ²)m | 目标仓位 | (关系留档,本文档 §1) | — |
| B.2 恒等式 | losscon_zz≡losscon | `econ.losscon_zz` | 数值一致断言 |
| 约束截断 | z_cap=cap·V̂/‖target‖ | `policy.s_opt_constrained` | §7.12-4 单测 |
| 图 2 | S 形曲线族 | `evaluation/figs.fig2_s_curve` | `fig2_s_curve.png` |
| 图 3 | 损失面 | `models/econ.fig3_loss_surfaces` | `fig3_loss_surfaces.png` |
