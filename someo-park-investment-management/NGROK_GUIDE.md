# Cloudflare Tunnel 启动攻略

让 prod 网站 (someopark.web.app) 通过 Cloudflare Tunnel 访问本地 Express 服务器的数据。

> **注意**：之前用 ngrok，现在改用 cloudflared。原因：ngrok 免费版有 TLS 错误，cloudflared 更稳定。
> **注意**：cloudflared 每次重启 URL 都会变（随机域名），需要重新 build + deploy 前端。

## 架构说明

```
浏览器 → someopark.web.app (Firebase Hosting, 静态前端)
              ↓ API 请求
         Cloudflare Tunnel (随机域名，每次启动都变)
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

这会在 `localhost:3001` 启动 Express 服务器（tsx watch 模式，改文件自动重启）。

### 2. 启动 Cloudflare Tunnel，获取新 URL

新开一个终端：

```bash
cloudflared tunnel --url http://localhost:3001 --logfile /tmp/cloudflared.log &
sleep 8 && grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/cloudflared.log | tail -1
```

几秒后会输出类似：
```
https://pregnancy-refresh-bloggers-authors.trycloudflare.com
```

复制这个 URL。

### 3. 更新 .env

编辑 `someo-park-investment-management/.env`，把 `VITE_API_URL` 改成新 URL：

```env
VITE_API_URL=https://pregnancy-refresh-bloggers-authors.trycloudflare.com
```

（URL 每次启动都不同，必须更新）

### 4. 重新构建前端并部署

```bash
cd ~/code/someopark-test/someo-park-investment-management
npm run build
firebase deploy --only hosting
```

> `VITE_*` 变量是构建时注入的，改了之后必须重新 build 才能生效。

### 5. 验证

访问 https://someopark.web.app 确认所有 artifact 正常加载。

---

## 快速操作（每次重启 tunnel 的完整流程）

```bash
# 步骤1: 确认 server 在跑
lsof -i :3001

# 步骤2: 杀掉旧 tunnel，启动新的
pkill cloudflared
cloudflared tunnel --url http://localhost:3001 --logfile /tmp/cloudflared.log &
sleep 8 && grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/cloudflared.log | tail -1

# 步骤3: 把输出的 URL 更新到 .env 的 VITE_API_URL

# 步骤4: build + deploy
cd ~/code/someopark-test/someo-park-investment-management
npm run build && firebase deploy --only hosting
```

---

## 常见问题

### Q: prod 网站上 artifact 显示 "Failed to fetch" / "Failed to load data"
- Cloudflare tunnel URL 变了 → 按上面流程重新启动 tunnel + 更新 .env + build + deploy
- 确认 Express 服务器还在跑：`lsof -i :3001`

### Q: prod 网站上 artifact 显示 401 Unauthorized
- 确认 `.env` 中 `VITE_API_KEY` 和 `SP_API_KEY` 值一致

### Q: 改了服务器代码但没生效
- `npm run server` 是 watch 模式，文件改动会自动重启，不需要手动操作

### Q: 改了前端代码但 prod 没生效
- 需要重新 build + deploy：`npm run build && firebase deploy --only hosting`

### Q: 怎么确认当前 tunnel URL 是什么
```bash
grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/cloudflared.log | tail -1
```

### Q: 本地开发时需要 cloudflared 吗？
不需要。本地 `npm run dev:all` 启动后，Vite dev server (port 3000) 会自动代理 `/api/*` 请求到 Express (port 3001)。cloudflared 只是为了让 prod 网站 (someopark.web.app) 能访问本地数据。
