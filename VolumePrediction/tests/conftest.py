"""套件级防护(macOS libomp,2026-07-23)。

结论(实测三轮): pip-lightgbm 与 torch 的 libomp 同进程共存不可靠
(先后顺序+旗标下仍会死锁)——**唯一可靠方案=进程隔离**。
LightGBMModel 在检测到 torch 已载入时自动走子进程(见 models/ml.py);
测试中的 lightgbm 用例亦以子进程方式执行。本 conftest 仅保留旗标
以防单独运行 lightgbm-only 测试时的良性告警。
"""
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
