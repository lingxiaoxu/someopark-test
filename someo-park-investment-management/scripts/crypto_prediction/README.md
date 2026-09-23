# Crypto prediction frontend

加密货币预测市场是主站第五个 `crypto` 模式。共享 `Sidebar`、`ChatArea` 顶部运行环境条/欢迎区/输入框、`RightPanel` 外壳均使用原组件；点击第五项只原位切换中间的 `CryptoUpcoming` / `CryptoArtifactGrid` 与右侧的 `CryptoPanelContent`。黑色主题由 App 将 `html[data-mode]` 映射到现有 `prediction`，世界杯主题文件不修改。

首页是紧凑单列二元市场总览和默认四行双列八个 artifact，沿用世界杯网格 class；原设置启用卡片分类时才显示分类。FAVE/PFME 两处选择器使用同一个 `CryptoContext`。完整代码位置、数据口径与隔离边界见 [Plan 16 — 加密货币预测市场前端](../../../crypto-dev/16_prediction_market_frontend.md)。

收益以纸面策略评估为主：首页纸面摘要优先，收益页初始选择「纸面评估」；Kalshi Demo 是独立执行账本。当前订单/持仓/结算/执行质量明确只显示 Demo，市场报价来自 Prod 只读录制。Prod 收益标签仅展示「未接入」，不切换交易，也不制造零收益。未来须建立新的独立 Prod 数据合同并保留 Demo 历史，详见 Plan 16 第11节。

2026-09-15 已修复本模块输入框、原生下拉框和 Recharts 与全局 CSS layer 的优先级冲突；所有覆盖限定于本模块。健康页补充注册源码与磁盘的字节差异、保留缺口及累计限流证据。新快照不掩盖源时间；发布与页面各60秒，账户核验至少间隔180秒，是分钟级更新。交易进程没有因本轮修复而重启。

本轮已完成标准build与someopark Hosting发布，并在正式主站验证。若仍见旧UI，刷新页面一次：既有Hosting HTML缓存为一小时，保留该共享配置未修改；刷新已实测取得新资源。发布器PID目前记录在模块private目录，勿硬编码历史PID。

## Firebase Hosting 部署

在前端目录执行标准两行：

```bash
npm run build
firebase deploy --only hosting --project someopark
```

默认 `vite.config.ts` 同时构建 `index.html` 和 `crypto-markets.html`，输出到 Firebase 原配置的 `dist`，无需附加config。原 `package.json`、`.firebaserc`、`firebase.json` 不修改；只发布Hosting，不发布Functions/Firestore。

主入口为 [Someo Park](https://someopark.web.app/)，从共享侧栏第五项进入，点击不做整页跳转。旧 [crypto-markets.html](https://someopark.web.app/crypto-markets.html) 保留兼容：`src/crypto-markets/main.tsx` 也加载同一 `<App initialAppMode="crypto" />`、i18n和主站样式。旧 `CryptoApp.tsx`、`crypto.css`、`MarketOverview.tsx`、`vite.crypto.config.ts` 已移除；此前临时Hosting config已取消。

生产数据从既有 `VITE_API_URL` 的 `/data/crypto_prediction/snapshot.json` 读取，不携带账户或API key。只在crypto模式启用唯一60秒轮询，离开后清理定时器及在途请求；首页与详情共用快照。地址缺失、非法、HTTP或schema失败均明确显示，不能改读Firebase部署时的静态快照。

2026-09-15 共享壳版本已成功部署：普通build产物包含两个入口，Firebase确认142个文件、version finalized、release complete。原项目类型检查、本模块strict检查及5项合同测试均通过；浏览器已验证八项artifact×两个策略、共享右栏的拖拽/最大化/关闭、切策略后重开不串合约，以及线上主站原位切换。原四模式页面和主题分别检查通过，控制台error/warn为空。

## 本地运行

在仓库根使用已有 `someopark_run` Python 环境运行只读导出：

```bash
conda run -n someopark_run --no-capture-output python -B -m crypto_trading.ops.export_prediction_frontend --once
conda run -n someopark_run --no-capture-output python -B -m crypto_trading.ops.export_prediction_frontend --watch 60
```

第二条持续运行，60秒读取本地源，Demo账户完整核验至少间隔180秒。启动前先检查本模块exporter是否已在运行，不能重复启动，也不用修改或重启W7/W8。所有外部读取仅GET。公开快照只写 `public/data/crypto_prediction/`，私有缓存/PID/日志只写仓库根 `crypto_trading/trading_signals/frontend_prediction/`。

前端使用主站现有开发命令；已有主站开发服务时直接复用：

```bash
npm run dev
```

默认主站开发地址为 `http://localhost:3000/`，点击第五项进入。兼容路径为 `/crypto-markets.html`，仍是同一主App。不再使用独立配置或 `dist-crypto` 发布流程。本轮自有预览在3014运行同一个默认Vite配置，PID仍记录到本模块的preview.pid；未占用或重启其他开发服务。开发模式读取同源公开快照；生产模式读取实时API。每分钟导出的public文件可能触发Vite开发热重载，验证刷新时保留界面状态应使用普通build后的 `vite preview` 或线上版本。

## 类型检查与测试

健康提示分为当前需处理、待核验、近期记录；历史 gap/429 不表示当前停机。当前故障须有新鲜来源和独立状态证据，无成功读取时间不能称已恢复。源码差异保留历史，不覆盖注册哈希。详见 Plan 16 第13节；W8限流修复仍未加载，版本切换另见 crypto-dev/17_w8_source_revision_recovery.md。

原项目缺少React类型包。模块strict检查需要时可安装到专属工具目录，不修改根package、锁文件或依赖解析：

```bash
npm install --prefix scripts/crypto_prediction/typecheck-runtime --no-package-lock --no-save --ignore-scripts @types/react@19 @types/react-dom@19
```

在前端目录验证：

```bash
npm run lint
node_modules/.bin/tsc -p tsconfig.crypto.json --noEmit
node_modules/.bin/tsx --test scripts/crypto_prediction/validate.test.ts scripts/crypto_prediction/resultBooks.test.ts scripts/crypto_prediction/healthSemantics.test.ts
npm run build
```

`tsconfig.crypto.json` 严格检查加密模块的Context/Home/Panel/数据组件，排除 `src/crypto-markets/main.tsx` bootstrap，避免通过App导入把原全站强制纳入新增strict配置。原 `npm run lint` 仍检查完整站点。普通build必须生成两个HTML入口，不能用专属构建替代。

仓库根的数据测试：

```bash
conda run -n someopark_run --no-capture-output python -B -m pytest crypto_trading/tests/test_export_prediction_frontend.py -q -p no:cacheprovider
```

前序已通过25项Python数据测试及5项前端合同测试；这属于历史数据验证结果，不能替代本轮UI的实际验收。测试覆盖真实快照、策略身份、账本恒等式、unknown/null、失败拒绝以及生产实时地址选择。不能将测试样例写入当前真实快照。

本轮扩展后通过36项Python测试、10项前端测试，包含只读健康核验和独立账本视图边界。没有改变纸面或Demo收益计算。发布器只可替换其 `publisher.pid` 对应且命令完全匹配的进程，不重启W7/W8，也不可同时启动两个publisher。

## 浏览器验收

从主站第五项原位进入，核对原侧栏、顶部条、欢迎区、输入框以及黑色主题；与世界杯逐项比较。打开八个artifact并切换两个策略，验证共享RightPanel、Demo/纸面区分、入场/退出、48小时/全部、分页、精确ticker、选择器联动、关闭/最大化/面板调整、窄屏、过期及请求失败。旧兼容地址也应是同一App壳。离开crypto后不应继续周期请求加密快照。

股票、世界杯、Macro和足球分别切换及打开artifact，检查既有行为和控制台；不修改这些模块的业务实现。交易代码、配置、Demo镜像及观察进程不因本UI工作改变，Prod交易没有启用。
