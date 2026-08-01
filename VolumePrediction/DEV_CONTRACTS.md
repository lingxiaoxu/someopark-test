# VolumePrediction 开发契约(所有开发 agent 必须遵守)

## 纪律(Plan §〇)
- 写入仅限 `VolumePrediction/` 与 `price_data/volume_prediction/`;其余一切只读
- 测试输出一律 `/tmp/vp_tests/<模块>/`;绝不写策略生产目录
- 全包禁止 `import yfinance`(含间接);Polygon key 不得出现在日志/异常文本
- 当前 pairs 管道运行中: 单测只用小合成数据;重算力(全面板/训练)由主协调者统一调度
- 不简化不删减: 计划里每个函数/文件都要真实实现;困难自行解决并在 docstring 记录

## 公共接口
### 面板 schema(features → models 的唯一契约)
- `pd.DataFrame`,`MultiIndex(date: Timestamp, ticker: str)` 已排序
- 必备列: `V`(美元量=shares×vw), `v`(=log V), `ma5_v`, `eta`(=v−ma5_v, 目标)
- 特征列前缀: `tech_` `fund1_` `fund2_` `cal_` `earn_`(G3 分组)
- 缺失: 按 config `models.train.fill_policy`(paper=零填充 / legacy=中位数)处理后**无 NaN**

### 通用工具 `VolumePrediction/common.py`(主协调者提供)
- `load_config() -> dict`、`REPO`/`PKG`/`OUT` 路径常量、`get_logger(name)`
  (日志只进 VolumePrediction/logs/)、`TMP_TEST_DIR`

### 模型 API(models/*)
```python
class BaseModel:
    name: str
    def fit(self, X: pd.DataFrame, y: pd.Series) -> "BaseModel": ...
    def predict(self, X: pd.DataFrame) -> pd.Series: ...   # index 对齐
    def param_count(self) -> int: ...                        # G4 公式断言用
```
- 论文规格模型(G4)超参写死为论文值,不接受调参参数
- torch 模型: 5 seeds 平均由 evaluation/walkforward 调用侧控制,模型只管单 seed(接受 seed 参数)

### econ API
- `policy.s_opt(v_bar, mu) -> z*`(闭式解);`policy.losscon(v, z, mu) -> float`
- `objective.resolve(strategy=None, trade_type=None, objective=None) -> Profile`
- μ=∞ 用 `mu_source='inf'` 剖面,policy 走排程退化路径

### 输出
- 工件写 `VolumePrediction/outputs/`(tmp+rename 原子);注册表 `outputs/registry/registry.json`
- 每工件三戳: model_version / trained_through / generated_at

## 交叉引用
计划全文: `.claude/plan/systemic-strategies-plan/VOLUME_PREDICTION_MODULE_PLAN.md`
(§三 G 清单=论文精确规格;§5.7/7.12=profile;附录 A=58 步映射;附录 D=消费文件地图)
