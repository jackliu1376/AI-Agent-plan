# 部署到轻量应用服务器

目标：一台 4 核 4G / 40G SSD / 3Mbps 的腾讯云轻量，跑单进程 uvicorn + Nginx。

## 这台机器够不够

| 资源 | 实际用量 | 结论 |
|---|---|---|
| CPU | 单进程，几乎全程在等模型返回 | 4 核用不到 1 核 |
| 内存 | 约 150–250MB | 4G 严重过剩 |
| 磁盘 | 代码 + 依赖约 600MB，`web/static` 337KB | 40G 够用很多年 |
| 带宽 | 首屏 gzip 后 **73KB**，3Mbps 下约 0.2 秒 | 够 |
| 流量 | 300GB/月，按每次访问 80KB 算约 375 万次 | 用不完 |

**结论：配置远超需求。** 唯一要留意的是带宽峰值 —— 3Mbps 意味着同时 3–5 个人首次加载会互相抢，之后就靠浏览器缓存了。

不要因为"核多"就去开多 worker，见下面「必须单进程」。

## 系统版本：22.04 还是 24.04

两个都能跑，`bootstrap.sh` 会自动适配。差异只有两处：

| | Ubuntu 22.04 | Ubuntu 24.04 |
|---|---|---|
| 系统 Python | 3.10（不够） | 3.12（够） |
| 怎么拿到 3.11+ | uv 下载一份，装到 `/opt/cairn/.python` | 直接用系统的 |
| nginx | 1.18 | 1.24 |
| `http2 on;` 指令 | 不支持 | **也不支持**（要 1.25.1+） |
| 安全更新到 | 2027-04 | 2029-04 |

**注意 nginx 那一行。** 升级到 24.04 并不能解决 `http2 on;` 的兼容问题 —— 两个版本的 nginx 都太老。`deploy/nginx.conf` 里已经用了兼容写法（`listen 443 ssl http2;`），两个版本通用，不用改。

如果还没开始部署，重装成 24.04 是划算的：少一层 Python 版本管理，支持周期多两年，成本只是控制台点一次「重装系统」。**但如果已经部署好了，没必要为了这个重装** —— 22.04 上脚本会自动下载 3.11，跑起来没有任何区别。

重装会清空系统盘。有数据先备份，或者把 `/opt/cairn` 打个包。

## 前置：域名与备案

这是国内服务器最容易卡住的一步，先确认清楚：

- **境内地域**（北京/上海/广州等）：域名解析过来后，80/443 端口会被拦截，直到完成 ICP 备案（约 1–2 周）。**只有 IP 访问不受此限。**
- **境外地域**（中国香港/新加坡等）：不需要备案，直接可用。

如果暂时不想备案，两条路：先用 `http://IP:80` 顶着（明文传输，API key 和任务内容都在裸奔，不建议长期），或者把服务器换成境外节点。

## 部署步骤

### 1. 传代码

```bash
# 服务器上
sudo mkdir -p /opt/cairn
sudo chown $USER:$USER /opt/cairn
git clone https://github.com/你的账号/AI-Agent.git /opt/cairn
cd /opt/cairn/task-planner
```

`web/static`（前端产物）和 `seed_generated.py`（景点种子）都在仓库里，**服务器不需要装 Node，也不用预生成景点库** —— `attractions.db` 会在第一次查询时自动建（`seed.connect()` 里有 `_needs_reseed` 检测）。

### 2. 写 .env

```bash
cp .env.example .env
nano .env      # 至少填 DEEPSEEK_API_KEY
```

**在 Windows 上编辑过的话，务必转成 LF**：

```bash
sed -i 's/\r$//' .env
```

systemd 的 `EnvironmentFile` 不会剥掉 `\r`，key 会变成 `sk-xxx\r` 发给上游，报 401 且完全看不出原因。`bootstrap.sh` 会自动处理这一步。

### 3. 一键装依赖 + 注册服务

```bash
sudo bash deploy/bootstrap.sh
```

做四件事：装 uv 与 Python 3.11、建 `cairn` 系统用户、`uv sync --no-dev`、装 systemd unit 并启动。

### 4. Nginx + 证书

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp deploy/nginx.conf /etc/nginx/sites-available/cairn
sudo sed -i 's/example.com/你的域名/g' /etc/nginx/sites-available/cairn
sudo ln -sf /etc/nginx/sites-available/cairn /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d 你的域名
```

没有域名的话，用 `nginx.conf` 末尾注释掉的那个 IP 版本。

### 5. 防火墙

两处都要开，只开 22 / 80 / 443：

```bash
# 系统内
sudo ufw allow 22,80,443/tcp && sudo ufw enable

# 腾讯云控制台 → 轻量应用服务器 → 防火墙，同样只放行这三个
```

**8000 绝对不要放行。** 后端只听 `127.0.0.1`，外部一律经 Nginx。

## 必须单进程

`deploy/cairn.service` 里是 `ExecStart=... python -m web.serve`，没有 `--workers`。

原因是 `Session`、限流计数器、`_janitor_loop` 全在进程内存里。开 4 个 worker 会变成四份互不相通的副本：SSE 订阅落在 A、`POST /answers` 落到 B，直接 404 或永久挂起。4 核用不上是正常的，这不是浪费。

将来真要横向扩展，得先把这三样外置（Redis 或数据库），那是另一个工程量。

## 四个配置项不能漏

```bash
WEB_HOST=127.0.0.1        # 不要 0.0.0.0
WEB_TRUST_PROXY=1         # 否则限流把所有用户算成同一个 IP
WEB_ACCESS_TOKEN=...      # 不设等于没有鉴权，见下一节
SESSION_DB_PATH=/opt/cairn/task-planner/logs/sessions.db
```

`WEB_TRUST_PROXY=1` 的作用：限流默认按 `request.client.host` 计数，而经过 Nginx 之后这个值永远是 `127.0.0.1`。不设的话一个人就能把全站额度吃光，其他人全部被限流。

## 部署后必须做的两件事（高德）

### 一、配域名白名单

去 https://console.amap.com/dev/key/app ，找到**服务平台为「Web端(JS API)」**的那个 key（不是「Web服务」那个），在域名白名单里加上你的域名：

```
你的域名.com,www.你的域名.com
```

只填域名主体，不带 `https://`、不带端口。多个用逗号分隔，支持 `*.你的域名.com`。本地开发用的 `localhost` 和 `127.0.0.1` 也建议保留。

不加的话地图会加载失败 —— 但不会白屏，前端 6 秒后降级成 SVG 示意图（能看点位关系，没有街道底图）。

### 二、让安全密钥走代理

`.env` 里加一行：

```bash
AMAP_SERVICE_HOST=/_AMapService
```

然后重跑 `sudo bash deploy/bootstrap.sh`（会生成 Nginx 的代理片段），再 `sudo nginx -t && sudo systemctl reload nginx`。

**为什么值得做。** 不配的话 `AMAP_JS_SECURITY_CODE` 会通过 `/api/map/config` 明文下发到浏览器，高德官方明确说「不建议在生产环境使用」。配了之后：

- 后端不再返回密钥（`security_code` 字段为空）
- 前端只知道往 `/_AMapService` 转发
- 密钥由 Nginx 在服务端拼进上游请求，浏览器拿不到

这样即使 key 被人从页面里扒走，也脱离不了你的域名（白名单）和配额。

**本地开发不要设它。** Vite 没有 `/_AMapService` 代理，设了本地地图会加载失败。本地保持留空，走明文模式即可 —— 那是你自己的机器。

## 安全：必须设置访问令牌

不设令牌时，`web/app.py` 的 11 个接口**全部开放**：

- `POST /api/sessions` —— 任何人提交任务都在烧你的模型余额
- `GET /api/sessions` —— 列出你所有的历史任务原文
- `DELETE /api/sessions/{id}` —— 删你的会话

限流默认 10 次/窗口，防的是刷，不是用。

### 设置方法

```bash
# 生成一个随机令牌
openssl rand -base64 24

# 写进 .env
WEB_ACCESS_TOKEN=刚才生成的值
```

```bash
sudo systemctl restart cairn
```

浏览器首次打开会看到令牌输入页，验证通过后换一个 HttpOnly cookie（有效期 7 天）。换掉 `WEB_ACCESS_TOKEN` 会让所有已登录的浏览器失效，需要重新输入。

### 实现要点（排查时用得上）

**凭证为什么走 Cookie 而不是 `Authorization` 头。** 前端订阅事件流用的是浏览器原生 `EventSource`，它**无法设置自定义请求头**。只做 header 鉴权的话事件流会被 401 挡在门外，页面卡在「正在查证」永不结束。Cookie 由浏览器对同源请求自动携带，`EventSource` 也吃这一套。

`Authorization: Bearer <token>` 同样有效，用 curl 调试时更方便：

```bash
curl -H "Authorization: Bearer $WEB_ACCESS_TOKEN" https://你的域名/api/sessions
```

**Cookie 里不放原始令牌**，存的是 `HMAC-SHA256(token, "cairn-auth-cookie-v1")`。这样即使 cookie 被日志、截图、代理访问记录带走，也拿不到能配置到别处的原始令牌。代价是无法单独吊销某个 cookie。

**哪些路径不需要令牌**：`/api/health`（探活）、`/api/auth/*`（登录本身）、以及所有非 `/api` 开头的静态资源（否则登录页都加载不出来）。

**`Secure` 标志按协议自动判断**：走 HTTPS 才加。本地 `http://localhost` 如果带了 `Secure`，浏览器会静默丢弃这个 cookie，表现为「提示登录成功、刷新又回登录页」，控制台没有任何报错。

### 可选：再叠一层 Basic Auth

应用层鉴权已经够了。如果还想加一层（比如在拿到令牌之前就先挡住扫描器），`nginx.conf` 里有两行注释掉的 `auth_basic`，取消注释即可：

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd 你的用户名
```

缺点是浏览器会弹两次框（一次 Basic、一次应用内），一般没必要。

## 验证清单

```bash
# 1. 服务活着
curl -s http://127.0.0.1:8000/api/health

# 2. 首页能出（应有 200 和 text/html）
curl -sI https://你的域名/

# 3. gzip 生效（应有 content-encoding: gzip）
curl -sI -H 'Accept-Encoding: gzip' https://你的域名/assets/index-*.js | grep -i encoding

# 4. SSE 不被缓冲（应逐条吐出，不是最后一次性全来）
curl -N https://你的域名/api/sessions/任意id/events

# 5. 跑一次真实任务，确认地图、澄清、修订都正常
```

## 常见问题

**服务起不来，日志说 `DEEPSEEK_API_KEY` 缺失**
`.env` 不存在或变量名拼错。`journalctl -u cairn -n 50` 看具体报错。

**401 / 403**
九成是 `.env` 的 CRLF 问题，跑 `sed -i 's/\r$//' .env && systemctl restart cairn`。

**地图一直转圈然后变成示意图**
高德 key 的域名白名单没加你的域名，或者 `AMAP_JS_SECURITY_CODE` 没填。

**前端加载很慢**
检查 `gzip on` 是否生效（`curl -I` 看 `content-encoding`）。3Mbps 下不压缩是 337KB，压缩后 73KB，差 4 倍。

**SSE 一次性全部返回**
Nginx 的 `proxy_buffering off` 没生效，检查是否改到了正确的位置块。

**历史会话打不开**
`logs/sessions.db` 权限不对。`chown -R cairn:cairn /opt/cairn/task-planner/logs`。
