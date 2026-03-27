# ngrok 隧道启动攻略

让 prod 网站 (someopark.web.app) 通过 ngrok 隧道访问本地 Express 服务器的数据。

## 架构说明

```
浏览器 → someopark.web.app (Firebase Hosting, 静态前端)
              ↓ API 请求
         ngrok 隧道 (固定域名)
              ↓
         本地 Express 服务器 (localhost:3001)
              ├── /api/*     → 各种后端路由
              └── /data/*    → 静态数据文件 (public/data/)
```

## 启动步骤

### 1. 启动本地 Express 服务器

```bash
cd ~/code/someopark-test/someo-park-investment-management
npm run server
```

这会在 `localhost:3001` 启动 Express 服务器 (tsx watch 模式，改文件自动重启)。

如果想同时启动前端 dev server (localhost:3000)：
```bash
npm run dev:all
```

### 2. 启动 ngrok 隧道

用固定域名启动，这样不用每次改前端配置：

```bash
ngrok http 3001 --domain=rarefactional-lifelessly-avril.ngrok-free.dev
```

启动后会看到类似输出：
```
Forwarding  https://rarefactional-lifelessly-avril.ngrok-free.dev → http://localhost:3001
```

### 3. 确认 .env 配置正确

检查 `someo-park-investment-management/.env` 中以下三个值：

```env
SP_API_KEY=cdf611a4506bb496f72de52262770f5c
VITE_API_KEY=cdf611a4506bb496f72de52262770f5c
VITE_API_URL=https://rarefactional-lifelessly-avril.ngrok-free.dev
```

- `SP_API_KEY` — 服务器端用，收到请求时校验 `x-api-key` header
- `VITE_API_KEY` — 前端用，发请求时带上 `x-api-key` header
- `VITE_API_URL` — 前端用，所有 API 请求的 base URL（指向 ngrok 隧道）

> 这三个值已经配好了，一般不需要改。只有在更换 ngrok 域名时才需要更新 `VITE_API_URL`。

### 4. 重新构建前端并部署

如果改了 `.env` 中的 `VITE_*` 变量，必须重新 build + deploy：

```bash
cd ~/code/someopark-test/someo-park-investment-management
npm run build
firebase deploy --only hosting
```

> `VITE_*` 变量是构建时注入的，改了之后必须重新 build 才能生效。

### 5. 验证

打开浏览器访问以下 URL 确认连通性：

```bash
# 测试 health check
curl https://rarefactional-lifelessly-avril.ngrok-free.dev/api/health

# 测试静态数据文件
curl https://rarefactional-lifelessly-avril.ngrok-free.dev/data/strategy_performance.json

# 带 API key 测试（如果 SP_API_KEY 已设置）
curl -H "x-api-key: cdf611a4506bb496f72de52262770f5c" \
  https://rarefactional-lifelessly-avril.ngrok-free.dev/api/health
```

然后访问 https://someopark.web.app 确认所有 artifact 正常加载。

## 快速启动（一行命令）

在项目目录下，两个终端分别执行：

```bash
# 终端 1: Express 服务器
cd ~/code/someopark-test/someo-park-investment-management && npm run server

# 终端 2: ngrok 隧道
ngrok http 3001 --domain=rarefactional-lifelessly-avril.ngrok-free.dev
```

## 常见问题

### Q: prod 网站上 artifact 显示 404
- 确认 Express 服务器正在运行 (`lsof -i :3001`)
- 确认 ngrok 隧道正在运行 (`curl https://rarefactional-lifelessly-avril.ngrok-free.dev/api/health`)
- 如果是 `/data/*` 路径 404，确认 `server/index.ts` 有静态文件中间件

### Q: prod 网站上 artifact 显示 401 Unauthorized
- 确认 `.env` 中 `VITE_API_KEY` 和 `SP_API_KEY` 值一致
- 注意：`/data/*` 路径不受 API key 保护（中间件只拦截 `/api/*`）

### Q: 改了代码但 prod 没生效
- 服务器代码改动：`npm run server` 是 watch 模式，会自动重启
- 前端代码改动：需要 `npm run build && firebase deploy --only hosting`
- `.env` 中 `VITE_*` 变量改动：同上，需要重新 build + deploy

### Q: ngrok 域名变了怎么办
1. 更新 `.env` 中的 `VITE_API_URL`
2. `npm run build && firebase deploy --only hosting`

### Q: 本地开发时需要 ngrok 吗？
不需要。本地 `npm run dev:all` 启动后，Vite dev server (port 3000) 会自动代理 `/api/*` 请求到 Express (port 3001)。ngrok 只是为了让 prod 网站 (someopark.web.app) 能访问本地数据。
