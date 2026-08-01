"""
features/intraday_features — 日内量分布形态特征(P2 可选增强)
============================================================
**状态: P2 可选,不进 58 步/G 复现主线**(计划 §四树注记/§6.3)。

数据源(实测存在,只读): `someopark.stock_data_hour`(7.1M 条小时线,2,665 票)。
用途草案(P2 弱点④"特征信息不足"弹药,留档不实现):
  intraday_shape(panel, mongo)  → 日内量 U 形偏度/首末小时占比/午盘干涸度
  open_auction_share(...)       → 开盘时段量占比(执行排程的日内先验)
"""
from __future__ import annotations

_DEFER_MSG = "P2-optional per plan §6.3 (not in replication mainline)"


def intraday_shape(*args, **kwargs):
    raise NotImplementedError(_DEFER_MSG)


def open_auction_share(*args, **kwargs):
    raise NotImplementedError(_DEFER_MSG)
