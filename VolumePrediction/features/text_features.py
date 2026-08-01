"""
features/text_features — 文本/情绪特征(弱点⑦ + I58)
====================================================
**状态: 按计划 §1.1 用户令整体推迟** —— 先完成 Volume Alpha 其余全部,本线挂起待批。

推迟依据(计划 §1.1/§6.1):
- 主源 `someopark.fmp_historical_social_sentiment`(15.7M 条,2024-10→今,~1.1K 票,
  stocktwits+twitter posts/comments/likes/impressions 8 字段)
- `fmp_stock_news_sentiments_rss_feed` 实测为 2025-03-06 单日快照 → 仅静态补充
- 自有 news*(~450K)日期覆盖待 P0 审计项 8(随本线一并推迟)
- 覆盖限制: 时间(2024-10 起)+ 标的(~1.1K 票)双重 availability mask(§6.8-SUE 同款
  幸存者掩码纪律)——重启时按"含/不含 survivor-only 特征"双口径报告

重启时的设计草案(留档,不实现):
  daily_sentiment(panel, mongo)   → text_sent_{stocktwits,twitter}_{posts,engagement}
  novelty(panel, mongo)           → 新闻新颖度(与近 N 日主题距离)
  lda_topics(corpus, k)           → I58 LDA 主题分布(需 gensim,随本线安装)
"""
from __future__ import annotations

_DEFER_MSG = "deferred per plan §1.1 (text/sentiment line postponed by user order)"


def daily_sentiment(*args, **kwargs):
    raise NotImplementedError(_DEFER_MSG)


def novelty(*args, **kwargs):
    raise NotImplementedError(_DEFER_MSG)


def lda_topics(*args, **kwargs):
    raise NotImplementedError(_DEFER_MSG)
