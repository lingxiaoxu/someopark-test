# fund-2 特征映射表(G3 验收物;JKP 分类 → 我方可计算实现)

依据: Jensen-Kelly-Pedersen (2022) 公开特征分类 13 主题。逐主题标注
[可算=本期实现 / 近似=口径差留档 / 不可得=数据缺失原因]。
目标 ≥60;本表 v1 实现 62 个(价量派生 21 + 年报派生 31 + 行业 10)。
数据源: Polygon 原始 bar+financials(PIT=filing_date,含退市) / FMP 年报
(acceptedDate PIT,现存票) / 分类=SIC。

## 主题 × 特征(62)

**1. Momentum(6, 可算, 价量)**: mom_12_1(附录B) / mom_6_1 / mom_3_1 / mom_1_0
/ mom_36_12(长期反转) / mom_seasonal_11(同月历史均值)
**2. Volatility(6, 可算, 价量)**: resvol_60(附录B) / vol_252 / vol_21
/ maxret_21(月内最大日收益) / zero_ret_frac_21 / beta_252(附录B,fund1 复用)
**3. Liquidity(5, 可算, 价量)**: amihud_20(附录B) / turnover_21(V÷市值)
/ dollar_vol_ma_126 / amihud_252 / turnover_vol_21(换手波动)
**4. Size(2, 可算)**: size_ln_mcap(fund1 复用) / ln_dollar_vol_126
**5. Value(5, 年报+价)**: be_me(fund1 复用) / earnyld(E/P) / sales_p(收入/市值)
/ fcf_p(自由现金流/市值) / debt_me
**6. Profitability(6, 年报)**: gross_profit_assets / roe / roa / roic_proxy
/ net_margin / operating_margin
**7. Investment/Growth(5, 年报 YoY)**: asset_growth / sales_growth / capex_growth
/ ppe_growth / equity_issuance_proxy(股本 YoY)
**8. Accruals/Quality(5, 年报)**: total_accruals_proxy((净利−经营现金流)/资产)
/ noa_assets / cash_assets / debt_assets / current_ratio
**9. Earnings/Analyst(3)**: sue(fund1 复用;幸存者掩码) / earn_streak_proxy(近4期
惊喜同号计数;掩码) / eps_growth(年报,可算)
**10. Payout(3, 年报)**: dividend_yield_proxy(分红/市值,Polygon dividends)
/ buyback_proxy(股本收缩) / total_payout_yield
**11. Industry(10, 可算)**: fund2_ind_SIC0..SIC9 哑变量(附录B)
**12. Short-term reversal(3, 价量)**: strev_5 / strev_21 / industry_rel_strev_21
**13. Seasonality/Misc(3)**: firm_age(fund1 复用) / etf 旗标(服务宇宙) / in_r3k

## 不可得声明(P0 留档)
- 分析师预期修订/离散度(JKP analyst 主题大部): 无历史 IBES 类源 → 以 SUE 系代理,
  survivor-masked 双口径报告(§6.8)
- 月频更新的季度报表字段: 年报-only 折让(§6.6);Polygon financials 季度化列 P2 升级
- 微观结构(bid-ask/PIN 等): 论文亦未用;不在范围

## 实现位置
价量派生 → data/factor_proxy.py + features/pipeline 扩展(P1 逐个落地并勾验);
年报派生 → inhouse_loader.annual_statement(income/balance/cashflow 字段映射表随代码);
本表为 P1 特征落地的检查单,每实现一个在行尾打 ✓(当前: 附录 B 7 个已实现 ✓)
