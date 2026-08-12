# μ 补两腿报告(E11-T2: pairs=OU half-life / ssrs=合成动量)

生成: 2026-08-12T16:05:25

## pairs(OU): μ = **1.166e-01**(source=ou_half_life, HL 中位 4.832673568772108,有效对 28/30)

## ssrs(合成动量): μ = **3.678e-05**(source=synthetic_momentum_decay, 事件 1894,面板天数 1909, alpha_by_delay={0: 0.001659, 1: 0.001631, 2: 0.001589, 3: 0.001548, 4: 0.001525, 5: 0.001528})

## 六 profiles 新旧对照
| profile | mode | μ 旧 | 来源旧 | μ 新 | 来源新 |
|---|---|---|---|---|---|
| aiss_emergency | urgent | inf | inf | inf | inf |
| aiss_rebalance | tracking | 0.0028244634881703216 | alpha_decay_curve | 0.0028244634881703216 | alpha_decay_curve |
| pairs_entry | event | 1e-06 | paper_prior | 0.11655941266328614 | ou_half_life |
| pairs_exit | event | 1e-06 | paper_prior | 0.11655941266328614 | ou_half_life |
| pairs_stop | urgent | inf | inf | inf | inf |
| ssrs_rebalance | tracking | 1e-06 | paper_prior | 3.677531751589526e-05 | synthetic_momentum_decay |

注: aiss_mom_decay 与 lambda_all 键本 runner 不动;消费切换仍待 8/15 E1 决策(μ 值仅影响 shadow 工件)。