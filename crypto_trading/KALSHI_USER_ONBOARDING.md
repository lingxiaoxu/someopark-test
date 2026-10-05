# Kalshi 用户实盘跟单:凭证接入与开通手册

最后更新 2026-10-05(加入精确成交同步)。仓库是公开的:本文件含一位真实用户的 Supabase id,**不要提交**。所有凭证都在仓库外的 `~/.kalshi/`。

## 0. 一页总览

```
用户(网页面板「API 密钥」)
  保存 API Key → 上传私钥文件 → 检测并连接 → 申请实盘跟单(选比例 + 勾风险确认)
主账户(本机,手动)
  review 审核 → ① 写批准记录 → ② .env 名单 → ③ 安全时段重启 livewatch → 核对
W7 运行端(livewatch)
  只给满足全部条件的用户镜像下单,张数 = 主账户张数 × 比例(向下取整)
任何时候
  用户在面板「停止跟单」或主账户写停止标记 → 下一笔起不再下单,无需重启
```

| 角色 | 做什么 | 写哪些文件 |
|---|---|---|
| 用户,通过网页面板 | 保存 key id、上传私钥、检测、申请、撤回、停止、删除 | 由网页后端代写:`pending/`、`prod_<uid>.pem`、`users.env`、`users.json`、`trading/requests/`、`trading/stopped/`、`audit.jsonl` |
| 网页后端 `someo-park-investment-management/server` | 校验和存储;**永远不会开通交易** | 同上 |
| 主账户 | 审核、开通、改比例、停止 | `trading/approved/<uid>.json`、`crypto_trading/.env` 的名单行、重启 livewatch |
| W7 运行端 livewatch | 镜像下单 | `journal/`、`disabled/<uid>`(遇 401/403)、`trading_active.json` |
| 导出器 `ops/export_prediction_frontend.py` | 生成每个用户的私有账本 | `ledgers/<uid>.json` |
| 精确成交同步 `ops/prod_fill_sync.py`,LaunchAgent `com.someopark.crypto.prodfillsync` 每 5 分钟 | 只读拉主账户和每个被跟单用户的逐笔成交 | `fills/<uid>.json`;主账户的在 `trading_signals/frontend_prediction/prod_fills_owner.json` |

`<uid>` 永远是 Supabase `auth.users.id`(36 位小写 UUID)。同一个用户在所有文件里都是同一个 id、同一个邮箱。

## 1. 文件结构

全部在仓库外,目录权限 700,文件权限 600。

```
~/.kalshi/
├── users.json                  注册表:检测通过后写入
├── users.env                   每个用户的 key id 和私钥路径:检测通过后写入
├── prod_<uid>.pem              用户私钥。上传什么文件名都存成这个名字;Ed25519 或 RSA
├── pending/
│   ├── <uid>.json              已保存、还没检测的 key id
│   └── <uid>.pem               已连接用户换 key 时,还没检测的新私钥
├── disabled/<uid>              下单遇 401/403 时运行端写入;用户重新检测后清除
├── trading/
│   ├── requests/<uid>.json     用户的实盘跟单申请(网页写)
│   ├── approved/<uid>.json     主账户的批准记录(只能主账户手写)
│   └── stopped/<uid>.json      停止标记(用户在面板写,或主账户手写)
├── trading_active.json         运行中的 W7 启动时发布的名单;网页据此显示「已开通」
├── journal/<UTC 日期>.jsonl     每个用户的每笔镜像下单(运行端写)
├── ledgers/<uid>.json          每个用户的私有账本(导出器写,只给本人看)
├── fills/<uid>.json            精确成交缓存:只读同步任务每 5 分钟从 Kalshi 拉该用户的逐笔成交,
│                               网页账本用它计算每笔的成本和手续费(回执只有 4 位小数)
└── audit.jsonl                 事件审计日志,只追加,不含密钥
```

仓库这边只有一行和用户有关:

```
crypto_trading/.env   (gitignored)
KALSHI_PROD_TRADING_USER_IDS=<uid>,<uid>      主账户的实盘名单;W7 进程启动时读取
```

主账户自己的 key 用不带后缀的 `KALSHI_PROD_*` / `KALSHI_MARGIN_*`,放在仓库的 .env 文件里,永远不进 `~/.kalshi/`。用户的条目永远带 `_<uid>` 后缀,不可能覆盖主账户。

## 2. 各文件内容

`users.json`:

```json
{
  "<uid>": {
    "user_id": "<uid>", "email": "...", "email_confirmed": true, "provider": "google",
    "status": "active", "validated_at": "...", "first_validated_at": "...", "supabase_checked_at": "...",
    "key_id_masked": "8a0acb3e…effc",
    "account_key_hashes": ["这个 Kalshi 账户上每个 key id 的 sha256"]
  }
}
```

`users.env`:

```
KALSHI_PROD_API_KEY_ID_<uid>=<key id>
KALSHI_PROD_PRIVATE_KEY_PATH_<uid>=/Users/xuling/.kalshi/prod_<uid>.pem
```

`trading/requests/<uid>.json`,用户申请:

```json
{"user_id": "<uid>", "email": "...", "ratio": 1, "ack_version": "2026-10-04", "requested_at": "..."}
```

`trading/approved/<uid>.json`,主账户批准。运行端只认两个字段:`user_id` 必须等于文件名,`ratio` 必须在 0 到 1 之间(含 1)。

```json
{"user_id": "<uid>", "ratio": 1}
```

`trading/stopped/<uid>.json`,停止标记:

```json
{"user_id": "<uid>", "by": "user", "reason": "panel", "stopped_at": "..."}
```

`trading_active.json`,运行中的 W7 发布:

```json
{"user_ids": ["<uid>"], "pid": 12345, "published_at": 1791200000.0}
```

`journal/<UTC 日期>.jsonl`,每笔一对行,`mirror_id` 相同:先 `pending_send`,再 `live_sent`(带交易所回执)。也可能是 `skipped_below_one_contract`(比例后不足 1 张)或 `skipped_by_flow_gate`。字段里 `contracts` 是用户自己的张数,`owner_contracts` 是主账户张数,`ratio` 是比例。

`audit.jsonl` 的事件:`key_id_saved`、`pem_uploaded`、`verified`、`verify_failed`、`verify_rejected`、`email_synced`、`disconnected`、`trading_requested`、`trading_request_cancelled`、`trading_stopped`。

## 3. 用户侧:接入凭证(网页)

1. 在 Kalshi 网站的 API keys 页面新建 key。2026-10-01 起默认 Ed25519,私钥下载为 `<key 名>.txt`(PKCS#8 PEM 文本,约 119 字节)。老的 RSA key 可能是 `.pem` 或 `.key`,也接受。
2. 登录 https://someopark.web.app ,打开侧栏菜单「API 密钥」。面板支持中、英、西、法、日五种语言。
3. 填 API Key(36 位小写 UUID),点保存。
4. 选私钥文件,点上传。只接受单个文件,80 到 4096 字节,扩展名 `.txt`、`.pem`、`.key`。
5. 点「检测并连接」。后端依次做:
   - 按 id 回查 Supabase,确认邮箱一致且已验证;
   - 用这把 key 签名读一次余额;
   - 读出这个 Kalshi 账户上的全部 key id;
   - 一个 Kalshi 账户只能绑定一个平台账户,主账户的 Kalshi 账户不能被绑定;
   - 通过后写入 `users.env` 和 `users.json`,清除停用标记。
6. 面板显示「已连接」,平台改为显示用户自己账户的数据。**这一步不会开始交易。**

**换 key:** 在 Kalshi 新建 key,在面板保存新 key id、上传新文件,这时新文件进 `pending/`,不影响当前 key。点「检测并连接」,通过后才替换。最后再到 Kalshi 删除旧 key。不要点面板的「删除」,那会撤回申请,需要重新审核。

## 4. 用户侧:申请实盘跟单(网页)

- 面板「实盘跟单」区:选比例 10% / 25% / 50% / 100%,勾选风险确认,点「申请开通」。后端写入 `trading/requests/<uid>.json`,本机弹出通知。
- 状态依次是:未开通 → 已申请,等待平台审核 → 已批准,将在下一个交易间隙生效 → 已开通(比例 X%)。停止后显示「已停止」或「已由平台停止」。
- 申请中可以撤回。已开通时点「停止跟单」,再点一次确认,下一笔起生效,不需要重启。
- 已开通时不能直接改比例:先停止,再重新申请,由主账户重新审核。停止后重新申请,停止标记会保留,等主账户处理。
- 用户没有申请,就不能开通:申请里的风险确认是用户的同意记录。

## 5. 主账户:审核

下面的命令都是只读的,不写任何文件。

```
crypto_trading/ops/kalshi_users.sh pending                        # 等待审核的申请
crypto_trading/ops/kalshi_users.sh review <email>                 # 逐项检查 + 风险预览 + 打印手动步骤
crypto_trading/ops/kalshi_users.sh review <email> --ratio 0.25    # 按你指定的比例预览
crypto_trading/ops/kalshi_users.sh status                         # 所有用户的状态
crypto_trading/ops/kalshi_users.sh lookup <email>                 # 单个用户
crypto_trading/ops/kalshi_users.sh check                          # 所有地方是不是同一个 id 和邮箱
```

`review` 检查的项目:

- Supabase 账户存在,邮箱已验证;
- 凭证已通过面板检测,私钥文件权限 600,没有被停用;
- 没有和其他平台账户共用同一个 Kalshi 账户;
- 用户已在网页申请并勾选风险确认;
- 比例是 10%、25%、50%、100% 之一;
- 用这把 key、经过实盘镜像用的同一个 Python 客户端读余额成功,能看出是 Ed25519 还是 RSA 签名;
- 2 号交易实例的现金不少于 $30。加密 15 分钟合约只用 2 号实例里的钱,用户要在 Kalshi 网页上把钱放在那里。

风险预览按这个比例,用主账户自按表下单上线以来的实盘记录,估算每个 15 分钟窗口占用的资金,以及最差的连续 1 小时、24 小时。占用或亏损相对余额过大时会给出警告。

## 6. 主账户:开通(手动三步)

开通必须由主账户亲手操作。Claude 的权限检查会拦截它代写批准记录和名单。在 Claude Code 里,命令前加 `!` 就在会话里运行;在普通终端里去掉 `!`。

### 6.1 通用模板

把 `<UID>` 换成用户 id,`<RATIO>` 换成 `0.1`、`0.25`、`0.5` 或 `1`。

**第一步,写批准记录。** 随时可以做。

```
! mkdir -p ~/.kalshi/trading/approved && chmod 700 ~/.kalshi/trading ~/.kalshi/trading/approved && echo '{"user_id": "<UID>", "ratio": <RATIO>}' > ~/.kalshi/trading/approved/<UID>.json && chmod 600 ~/.kalshi/trading/approved/<UID>.json
```

如果这个用户以前停止过,同时删掉停止标记:

```
! rm -f ~/.kalshi/trading/stopped/<UID>.json
```

**第二步,加名单。** 随时可以做。名单只能有一行,多个 id 用逗号隔开。

名单里还没有任何用户时,追加一行:

```
! echo 'KALSHI_PROD_TRADING_USER_IDS=<UID>' >> ~/code/someopark-test/crypto_trading/.env
```

名单里已经有用户时,把新 id 接到同一行末尾。**不要再追加第二行**:读取时以最后一行为准,前面的用户会被挤掉。

```
! sed -i '' 's/^KALSHI_PROD_TRADING_USER_IDS=.*/&,<UID>/' ~/code/someopark-test/crypto_trading/.env
```

确认只有一行名单:

```
! grep '^KALSHI_PROD_TRADING_USER_IDS=' ~/code/someopark-test/crypto_trading/.env
```

**第三步,重启 livewatch。** 只能在安全时段做:每个 15 分钟收盘前 3.0 到 4.4 分钟,也就是每小时的 xx:10:40–xx:11:50、xx:25:40–xx:26:50、xx:40:40–xx:41:50、xx:55:40–xx:56:50。这个时段夹在本窗口最后一次入场和下一窗口第一次入场之间,重启不会碰上下单。名单只在 W7 进程启动时读取,所以必须重启。

```
! launchctl kickstart -k gui/$(id -u)/com.someopark.crypto.livewatch
```

不想自己掐时间,可以在普通终端里用这个版本。它会自动等到下一个安全时段再重启,最多等 15 分钟:

```
bash -c 'while :; do r=$((900 - $(date +%s) % 900)); [ $r -ge 200 ] && [ $r -le 255 ] && break; sleep 2; done; launchctl kickstart -k gui/$(id -u)/com.someopark.crypto.livewatch && date'
```

### 6.2 当前这位用户

2026-10-05 已申请,比例 100%,`review` 全部通过。

```
! mkdir -p ~/.kalshi/trading/approved && chmod 700 ~/.kalshi/trading ~/.kalshi/trading/approved && echo '{"user_id": "410340c8-38a1-477d-9071-17a9678a9725", "ratio": 1}' > ~/.kalshi/trading/approved/410340c8-38a1-477d-9071-17a9678a9725.json && chmod 600 ~/.kalshi/trading/approved/410340c8-38a1-477d-9071-17a9678a9725.json

! echo 'KALSHI_PROD_TRADING_USER_IDS=410340c8-38a1-477d-9071-17a9678a9725' >> ~/code/someopark-test/crypto_trading/.env

! launchctl kickstart -k gui/$(id -u)/com.someopark.crypto.livewatch
```

第三条只能在安全时段运行,见上面第三步。

## 7. 开通后核对

```
! ~/code/someopark-test/crypto_trading/ops/kalshi_users.sh status
```

这个用户那一行应显示「批准 100% / .env 名单内 / 正在跟单: 是」。另外可以看:

- `~/.kalshi/trading_active.json` 里的 `pid` 是新进程,`user_ids` 包含这个 id;
- 网页面板显示「已开通(比例 X%)」;
- W7 下一笔单之后,日志里出现用户下单结果:

```
! grep -E "allowlist published|USER ORDERS" ~/code/someopark-test/crypto_trading/logs/watch.log | tail -5
```

- `~/.kalshi/journal/<UTC 日期>.jsonl` 里出现成对的 `pending_send` 和 `live_sent` 行。
- 第一笔跟单后 5 分钟内,`status` 里这个用户下面出现「精确成交同步: N 分钟前」。新用户不需要任何额外操作:同步任务从跟单记录里自动找到所有被下过单的用户。

## 8. 运行端规则(W7 镜像下单)

- **谁会被下单。** 同时满足以下全部条件:
  - 在 `.env` 名单里,而且运行中的进程已经加载;
  - `users.json` 里 `status` 为 `active`;
  - `users.env` 两行都在,私钥文件存在;
  - 没有 `disabled/<uid>`;
  - 有批准记录,比例合法;
  - 没有停止标记;
  - key id 和私钥指纹都不是主账户的;
  - 这个 Kalshi 账户没有绑定到第二个平台账户。
- **什么时候下。** 主账户的单返回以后,在独立线程里并行发给各个用户。主账户永远先下单。
- **下多少。** 主账户张数乘以比例,向下取整,不超过主账户。不足 1 张就不下单,记一行 `skipped_below_one_contract`。价带、白天和夜间仓位、宏观发布降仓、每窗上限这些规则,都已经体现在主账户的下单意图里,用户跟同一个意图。
- **出错怎么办。** 遇到 401 或 403,只停用这一个用户,写 `disabled/<uid>`;用户在面板重新检测后恢复。余额不足等其他拒单只记进 journal,不停用。
- **什么时候生效。** 批准记录、停止标记、key 文件每一笔都重新读取,所以改比例和停止都从下一笔起生效。名单只在进程启动时读取。

## 9. 改比例、停止、移除

- **改比例。** 按第一步重写批准记录里的 `ratio`,下一笔起生效,不用重启。
- **主账户停止某个用户,立即生效:**

```
! echo '{"user_id": "<UID>", "by": "owner", "reason": "<原因>"}' > ~/.kalshi/trading/stopped/<UID>.json && chmod 600 ~/.kalshi/trading/stopped/<UID>.json
```

  然后把这个 id 从名单行里删掉,下次重启后彻底卸载。
- **恢复。** 删掉停止标记。只要批准记录还在、id 在名单里、运行中的进程已经加载,下一笔起就恢复。
- **用户在面板点「删除」。** 申请被撤回,key 和注册信息被删除。如果之前被批准过,会留下一个停止标记,用户重新连接后不会自动恢复跟单。

## 10. 排障

| 现象 | 原因和处理 |
|---|---|
| status 显示「正在跟单: 否」,但已批准、在名单内 | livewatch 还没重启;按第三步在安全时段重启 |
| check 报「没有批准记录」 | 漏了第一步 |
| 面板显示「认证失效,请重新检测」 | 下单遇 401/403 被自动停用;让用户在面板重新检测 |
| 用户成交比主账户少 | 主账户先吃掉了盘口,IOC 落空,这是正常的跟踪差异 |
| journal 里出现余额不足的拒单 | 比例相对余额过大;降比例,或请用户往 2 号实例入金 |
| 加第二个用户后,第一个用户不再跟单 | `.env` 里有两行名单,后一行覆盖了前一行;合并成一行后重启 |
| check 报「没有精确成交缓存」或「N 分钟没更新」 | 同步任务没跑或失败;看 `launchctl print gui/$(id -u)/com.someopark.crypto.prodfillsync` 和 `crypto_trading/logs/prod_fill_sync.log`。期间网页账本退回用回执估计,每笔误差不到 1 分钱 |
| 网页盈亏和 Kalshi 差几分钱 | 最近 5 分钟内的新单还没同步到精确成交;下一轮同步后自动对上 |

## 11. 安全注意

- 仓库公开。任何 key、私钥、用户邮箱都不进仓库,凭证只在 `~/.kalshi/`。
- 不要在聊天或截图里展示私钥。
- 开通只能主账户亲手操作。如果想让 Claude 代做重启,可以在 `/permissions` 的 Allow 里只放行这一条:`Bash(launchctl kickstart -k gui/501/com.someopark.crypto.livewatch)`。不建议长期放开对 `.env` 的编辑权限。
- 另一个办法:按 Shift+Tab 临时切到手动确认模式,每条命令都会弹窗,由你逐条批准,做完再切回 auto。

## 12. 代码位置

| 作用 | 文件 |
|---|---|
| 网页接口:状态、key、私钥、检测、余额、账本、申请、撤回、停止 | `someo-park-investment-management/server/routes/kalshiKeys.ts` |
| 存储、校验、签名、申请状态 | `someo-park-investment-management/server/utils/kalshiUserKeys.ts` |
| 面板界面 | `someo-park-investment-management/src/components/ApiKeysPanel.tsx` |
| 前端接口和比例常量 | `someo-park-investment-management/src/lib/kalshiUser.ts` |
| 用户筛选、批准比例、停止标记、名单 | `crypto_trading/crypto_common/config.py` |
| 镜像下单与按比例缩放 | `crypto_trading/crypto_common/execution_events.py`,`_mirror_user_accounts` |
| 启动时发布名单 | `crypto_trading/crypto_strategies/live_watch/runner.py`,`_publish_trading_allowlist` |
| 主账户的只读审核工具 | `crypto_trading/ops/kalshi_users.py` 和 `kalshi_users.sh` |
| 每个用户的私有账本 | `crypto_trading/ops/export_prediction_frontend.py`,`write_user_ledgers` |
| 精确成交同步,只读,主账户和所有被跟单用户 | `crypto_trading/ops/prod_fill_sync.py` 和 `prod_fill_sync.sh` |
| 测试 | `crypto_trading/tests/test_live_watch.py`、`tests/test_kalshi_users_ops.py`、`someo-park-investment-management/server/tests/kalshiUserKeys.test.ts` |
