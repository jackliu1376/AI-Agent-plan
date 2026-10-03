# 任务规划助手 · Task Planner

> 把一句"我想周末去成都玩两天"变成一份**有序、可执行、带依赖关系**的行动计划。
> 一个基于 **MCP 协议**的工具调用 Agent，会自己查天气、翻景点库、算交通，缺数据时如实说而不是编。

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-275%20passed-2ea44f)
![License](https://img.shields.io/badge/license-MIT-blue)
![Protocol](https://img.shields.io/badge/protocol-MCP-6E56CF)
![LLM](https://img.shields.io/badge/LLM-DeepSeek-4D6BFE)

---

## 这是什么

给它一句自然语言任务，它调用工具取证，然后产出一份结构化计划：

```
阶段(phase) → 步骤(step) → 动作(action) → 工具(tool) → 前置依赖(depends_on)
           → 预估耗时(eta) → 产出物(deliverable) → 验收标准(done_when)
```

同时输出 **Markdown 人类可读版** 和 **JSON 机器可解析版**，后者带依赖图校验（必须无环）。

它**只做规划，不做执行**——不订票、不付款、不发消息。

---

## Demo

### 网页版

<table>
<tr>
<td width="50%">

**① 输入需求**

![输入界面](docs/screenshots/01-input.png)

</td>
<td width="50%">

**② 实时进度**

用户能看到 Agent 正在查什么、花了多久 —— 而不是盯着一个转圈等 30 秒。

![实时进度](docs/screenshots/02-progress.png)

</td>
</tr>
<tr>
<td width="50%">

**③ 结构化计划**

阶段 → 步骤 → 依赖 → 数据来源，一眼看清哪些步骤能并行。

![计划视图](docs/screenshots/03-plan.png)

</td>
<td width="50%">

**④ 澄清提问**

信息不足时暂停并提问，而不是硬猜。回答后自动恢复。

![澄清卡片](docs/screenshots/04-clarification.png)

</td>
</tr>
</table>

### 命令行

真实运行（DeepSeek `deepseek-chat`），无任何剪辑：

```console
$ uv run task-planner "我想周末去成都玩两天，带小孩，人均预算 1000" -v
  · 第 1 轮：请求模型
  · 模型请求 2 次工具调用
  · ✅ get_weather_forecast (1435ms / 1 次尝试)
  · ✅ query_attractions_db (31ms / 1 次尝试)
  · 第 2 轮：请求模型
  · 模型给出最终回复

📋 计划（人类可读版）
# 成都周末亲子两日游 · 执行计划

**行程日期**：2026-10-03（周六）～ 2026-10-04（周日）
**天气实况（已查证）**：10-03 阵雨 17.8~25.3℃、降水概率 51%；
10-04 毛毛雨 17.5~20.8℃、降水概率 71%。
**两天降水概率均≥50%，必须准备室内备选方案**。

## 阶段一：行前准备
| 步骤 | 具体动作 | 工具 | 前置依赖 | 耗时 | 产出物 |
|---|---|---|---|---|---|
| S1 | 确认出行人数与儿童年龄，锁定总预算 | null | — | 0.2h | 人数/预算确认单 |
| S2 | 查询并记录成都天气，判定雨天备选 | get_weather_forecast | — | 0.2h | 天气结论 |
| S3 | 筛选亲子景点并排序 | query_attractions_db | — | 0.3h | 候选景点清单 |
...
| S11 | **Day2 上午（雨天主选）**：四川科技馆（免费，3h） | null | S2 | 3h | 游览记录 |
...

依赖图校验：✅ 无环
引用工具：estimate_route, get_weather_forecast, parse_budget_csv,
         query_attractions_db, save_itinerary

🔍 运行轨迹
[成功] 轮次=2 工具调用=2 (失败 0) 预算=2/12 停止原因=model_finished
  ✅ get_weather_forecast 1435ms
  ✅ query_attractions_db 31ms
```

注意它做的几件事：把"周末"**自己推算**成 `2026-10-03/04`（没反问用户）、
**真实调用**天气接口、发现两天降水概率都超 50% 后**主动切换室内方案**、
并且每个含事实的步骤都标了 `data_source`。

### 缺数据时它会说实话

```console
$ uv run task-planner "帮我规划这周末去伊斯坦布尔玩三天"
  ✅ get_weather_forecast  1258ms   ← 解析为「伊斯坦布尔 / 土耳其」
  ❌ NOT_FOUND query_attractions_db  ← 景点库不覆盖该城市

⚠️ **数据缺失**：本地景点库不覆盖伊斯坦布尔（仅覆盖三亚/东京/伦敦/北京/巴黎/成都/
新加坡/曼谷/杭州/纽约/西安/首尔 12 城），因此**无法提供景点清单与票价**。
计划中涉及具体景点的部分需你自行核实或改用官方渠道查询。
```

工具失败不会中断流程，Agent 继续用其他工具完成计划，并明确标注缺口——
**不编造景点、票价或坐标**。

---

## 核心特性

- **8 个功能互异的工具** —— 天气 API、网页抓取、SQLite 查询、CSV 解析、汇率换算、
  路径估算、文件落盘、用户澄清
- **MCP 协议桥接** —— 工具以标准 MCP Server（stdio / JSON-RPC）暴露，
  同时保留 LLM 原生 function calling 作为降级通道
- **单一 Schema 来源** —— 工具的 Pydantic 模型同时生成 MCP `inputSchema` 与
  LLM `tools` 参数，有测试断言两者逐字节一致
- **6 条运行时护栏** —— 轮次上限、工具预算、幂等去重、失败重试+降级、
  提示注入隔离、输出校验+修复重试
- **14,359 个中文城市名解析** —— 本地索引优先，覆盖 229 个国家/地区，中英文名与
  繁体输入都能命中
- **42 个城市 / 282 条景点** —— 覆盖中国全部 34 个省级行政区 + 7 个国际城市，
  含币种字段（USD/JPY/EUR/HKD/TWD…）
- **193 个测试，100% 通过** —— 含 21 个"曾经解析错城市"的回归用例

---

## 快速开始

需要 [uv](https://docs.astral.sh/uv/)（Python 3.11+）。

```bash
git clone <your-repo-url> && cd AI-Agent/task-planner
uv sync

# ① 零配置跑通全链路（脚本化 LLM，但工具真实执行）
uv run task-planner --demo -v

# ② 走 MCP 协议调用工具
uv run task-planner --demo --transport mcp -v

# ③ 接入真实 DeepSeek
cp .env.example .env      # 填入 DEEPSEEK_API_KEY
uv run task-planner "我想周末去成都玩两天，带小孩，人均预算 1000" -v

# ④ 跑测试
uv run pytest
```

> `--demo` 模式用一个脚本化 LLM 按剧本返回，**工具是真实执行的**。
> 因此没有 API Key 也能完整演示「模型决策 → 工具调用 → 结果回灌 → 产出计划」这条链路。

### 网页版

```bash
uv run task-planner-web      # → http://127.0.0.1:8000
```

前端产物已随仓库提交（`web/static/`），因此**不需要 Node 环境**即可使用。

若要改前端，需要 Node 18+：

```bash
cd frontend
npm install
npm run dev                  # → http://localhost:5173，/api 自动代理到 8000
```

开发时跑两个进程（后端 8000 + 前端 5173）；改完执行 `npm run build`，
产物直接输出到 `web/static/`，FastAPI 会自动托管，生产环境只需一个进程。

### 其他命令

```bash
uv run task-planner --list-tools                    # 查看已注册工具
uv run task-planner --demo --stream                 # 以事件流方式输出进度
uv run task-planner --demo --save chengdu.md        # 保存计划到 outputs/
uv run python -m mcp_server.server --list           # 打印 MCP 暴露的 Schema
```

---

## 事件流接口

编排循环的核心入口是 **`Orchestrator.run_stream()`** —— 一个异步生成器，
把整个规划过程拆成事件逐个产出：

```python
async for event in orch.run_stream("我想周末去成都玩两天"):
    print(event.type, event.data)      # 可直接 SSE 推给前端
```

| 事件 | 时机 | 关键字段 |
|---|---|---|
| `run_started` | 开始 | `max_turns` `tool_budget` |
| `turn_started` | 每轮模型调用 | `turn` `max_turns` |
| `tool_call` | 模型请求调工具 | `tool` `args` |
| `tool_result` | 工具返回 | `ok` `error_code` `latency_ms` `budget_used` |
| `clarification` | **需要用户补充信息** | `question` `options` `reason` |
| `repair` | 输出不合规触发重试 | `reason` |
| `plan_ready` | 产出最终计划 | `plan` `markdown` `trace` |

三个约定：

1. **`event.data` 全部 JSON 可序列化** —— SSE 可以原样推给前端，不需要为每种事件写转换。
   有测试逐事件断言这一点。
2. **澄清是「暂停 → 提问 → 恢复」**，不是中断。`ask_user` 回调同步（CLI 的 `input()`）
   或异步（Web 层挂起等待前端回填）都支持。
3. **`run()` / `run_async()` 只是事件流的消费者**，对外行为与改造前完全一致 ——
   重构时 193 个既有测试**一行没改**就全绿。

CLI 里可以直接看到效果：

```console
$ uv run task-planner --demo --stream
  ▶ 开始规划（轮次上限 8，工具预算 12）
  ◆ 第 1/8 轮：请求模型
  → 调用工具 get_weather_forecast {'city': '成都', ...}
    ✅ get_weather_forecast 1517ms
  → 调用工具 query_attractions_db {'city': '成都', 'tags': ['亲子']}
    ✅ query_attractions_db 7ms
  ◆ 第 2/8 轮：请求模型
  ✔ 计划就绪（10 个步骤）
```

---

## Web API

```bash
uv run task-planner-web            # → http://127.0.0.1:8000
```

交互式接口文档在 `/docs`。

### 路由

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/tools` | 工具清单（含 JSON Schema，前端可渲染表单） |
| `POST` | `/api/sessions` | 提交任务，返回 `session_id` |
| `GET` | `/api/sessions/{id}` | 查询状态 / 结果（刷新页面后恢复用） |
| `GET` | `/api/sessions/{id}/events` | **SSE 事件流**，支持 `?cursor=N` 续传 |
| `POST` | `/api/sessions/{id}/answers` | 提交澄清答案 |
| `DELETE` | `/api/sessions/{id}` | 结束并清理会话 |

### 为什么拆成两个请求？

`EventSource` 有两个硬限制：**只能发 GET**、**不能带自定义请求头**。
所以任务参数不能塞进流式请求里，必须「先 POST 建会话，再 GET 订阅」：

```console
$ curl -X POST localhost:8000/api/sessions -H 'Content-Type: application/json' \
       -d '{"task":"我想这周末去成都玩两天"}'
{"session_id":"b8e75acc...","events_url":"/api/sessions/b8e75acc.../events"}

$ curl -N localhost:8000/api/sessions/b8e75acc.../events
data: {"type":"run_started","data":{"max_turns":8,"tool_budget":12,...}}

data: {"type":"turn_started","data":{"turn":1,"max_turns":8}}

data: {"type":"tool_call","data":{"tool":"get_weather_forecast","args":{...}}}

data: {"type":"tool_result","data":{"tool":"get_weather_forecast","ok":true,"latency_ms":1216,"budget_used":1}}

data: {"type":"plan_ready","data":{"ok":true,"step_count":14,...}}
```

事件统一走默认的 `message` 事件（不设 `event:` 字段），前端一个 handler 按
`data.type` 分派即可。空闲时服务端每 15 秒发一行 `: ping` 注释保活。

### 澄清是「暂停」，不是中断

服务端协程挂在一个 `Future` 上，会话状态变为 `awaiting_input`；
前端弹窗提问，用户回答后 `POST /answers` 唤醒它继续跑：

```
SSE:  {"type":"clarification","data":{"question":"你想去哪里？",...}}
      ← 服务端在此挂起，状态 = awaiting_input
POST /api/sessions/{id}/answers  {"answer":"成都，10月3-4日"}
      → 协程恢复，继续调用工具直到产出计划
```

### 部署注意事项

| 项 | 说明 |
|---|---|
| **API Key** | 只存在于服务端，绝不下发前端。所有 LLM 调用都在后端完成 |
| **限流** | 默认 10 次 / IP / 小时（`WEB_RATE_LIMIT` 可调），防止被刷爆额度 |
| **会话回收** | TTL 30 分钟 + 上限 200 个，超限按最旧优先淘汰 |
| **事件循环** | `llm.chat()` 与工具调用都是同步的，已放进线程池（`asyncio.to_thread`）。否则一个请求会冻住整个服务 |
| **反向代理** | 已设置 `X-Accel-Buffering: no`，避免 Nginx 缓冲 SSE 导致事件攒着一起发 |
| **多实例** | `SessionStore` 是内存实现。要多副本部署需换成 Redis（接口不变） |

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEB_HOST` | `127.0.0.1` | 监听地址 |
| `WEB_PORT` | `8000` | 端口 |
| `WEB_RELOAD` | — | 设为 `1` 开启热重载 |
| `WEB_RATE_LIMIT` | `10` | 每 IP 每小时的建会话上限 |
| `WEB_TRUST_PROXY` | — | 设为 `1` 时按 `X-Forwarded-For` 限流（**仅前置可信代理时开启**，否则可被伪造绕过） |

---

## 前端

React 18 + TypeScript + Vite。**没有引入路由**——整个应用只有三个状态，
用状态机比路由更贴合。

```
frontend/src/
├─ api/
│  ├─ types.ts              前后端契约（事件 + Plan + Trace 的 TS 类型）
│  └─ client.ts             HTTP + SSE 客户端
├─ hooks/
│  └─ usePlanningSession.ts 核心状态机：提交 → 订阅 → 澄清 → 结果
├─ components/
│  ├─ TaskInput.tsx         输入框 + 示例任务
│  ├─ ProgressFeed.tsx      实时进度流
│  ├─ ClarificationCard.tsx 澄清提问与作答
│  └─ PlanView.tsx          计划时间线（含依赖关系标注）
└─ App.tsx
```

### 三个值得说明的实现细节

**1. 事件流订阅写在用户操作里，不写在 `useEffect` 里**

React 18 的 `StrictMode` 在开发模式下会「挂载 → 卸载 → 再挂载」。
如果把 `EventSource` 建在 effect 里，会开出两条连接、事件重复。
改成点击提交时才开流，StrictMode 就不会重复执行，不需要 ref 守卫这类补丁。

**2. SSE 断线重连要自己管游标**

浏览器原生 `EventSource` 自动重连时会重发**同一个 URL**，
而服务端的 `?cursor=N` 会过期。所以这里关掉自动重连，自己记录已收到的条数、
按退避策略重连并带上新游标。收到 `plan_ready` / `error` 即视为终态，不再重连。

**3. 类型是契约，不是装饰**

`types.ts` 里的 `PlanEvent` 是可辨识联合，`switch (event.type)` 时 `data` 会自动收窄。
后端若改了字段，先改这个文件，TS 编译器会把所有受影响的渲染代码指出来——
而不是等到运行时才发现某个字段是 `undefined`。

### 前端构建

```bash
cd frontend
npm run typecheck      # tsc --noEmit
npm run build          # 产物 → ../web/static/
npm run dev            # 开发服务器（5173，代理 /api → 8000）
```

---

## 桌面版

```bash
uv run task-planner-desktop     # 或双击 start-desktop.bat
```

打开一个原生窗口（无地址栏、无标签页），里面就是同一个界面。

### 为什么只有 80 行代码

因为架构已经天然适合：

- 前端是**静态产物**，运行时不需要 Node
- 后端**单进程**同时提供 API 与静态文件
- Web 层用的是 `LocalToolRunner`，**不依赖 MCP 子进程**

所以桌面版 = 启动本地服务 + 开一个窗口指向它，**不复制任何业务逻辑**。

```
┌─────────────────────────────┐
│  原生窗口（pywebview）        │  ← 新增，约 80 行
│  系统自带 WebView2 · 无地址栏  │
└──────────┬──────────────────┘
           │ HTTP + SSE
┌──────────▼──────────────────┐
│  FastAPI 服务（现有代码不动）  │
│  ├─ /api/*      业务接口      │
│  └─ /            前端静态产物  │
└─────────────────────────────┘
```

### 三个实现要点

**1. 端口从已绑定的 socket 读回**，而不是「先探测空闲端口再绑定」

后者两步之间有竞态窗口，可能被别的进程抢走。这里的做法是让 uvicorn 绑 `port=0`，
启动后从 `server.servers[0].sockets[0].getsockname()` 读回真实端口，**完全没有竞态**。

**2. 服务跑在后台线程**

pywebview 的窗口循环必须占用主线程，所以 uvicorn 只能放线程里跑。

**3. 关窗即退出**

`webview.start()` 返回后必须显式设置 `server.should_exit = True`，
否则 uvicorn 线程不会退出，留下占着端口的僵尸进程。

### 已知限制

- **没有打包成 exe** —— 需要用户装 Python + uv。要做到「双击即用」还需要
  PyInstaller 打包 + 路径改造（见下）。
- **API Key 仍写在 `.env`** —— 桌面用户友好的做法是加一个设置界面，
  存到 `%APPDATA%`，目前未做。
- 若后续要打包，有三处路径必须先改：`OUTPUT_DIR`（相对路径）、
  `attractions.db`（会写进临时解压目录）、`.env` 位置。

---

## 架构

```
┌──────────────┐
│   用户输入    │  自然语言任务描述
└──────┬───────┘
       ▼
┌────────────────────────────────────┐        ┌──────────────────┐
│  编排循环 Orchestrator              │◄──────►│  DeepSeek        │
│  解析→澄清→取证→分解→排序→校验→输出  │ 函数调用│  deepseek-chat   │
│  护栏：轮次≤8 预算≤12 去重 重试 降级 │        └──────────────────┘
└──────┬─────────────────────────────┘
       ▼
┌────────────────────────────────────┐
│  MCP 桥接层（stdio / JSON-RPC）     │
│  MCPToolRunner ──► MCPServer       │
│  （降级：LocalToolRunner 进程内直调）│
└──────┬─────────────────────────────┘
       ▼
┌──────────┬──────────┬──────────┬──────────┐
│ 天气 API  │ 网页抓取  │ 本地SQLite│ CSV 解析  │
│ 交通估算  │ 汇率换算  │ 计划落盘  │ 用户澄清  │
└──────────┴──────────┴──────────┴──────────┘
       ▲
       │
┌──────┴─────────────────────────────┐
│ 数据层：cities.py(170) + city_index │
│ (14,359) + seed.py(282 条景点)      │
└────────────────────────────────────┘
```

---

## 工具清单

| ID | 工具 | 功能 | 类型 | 幂等 |
|---|---|---|---|---|
| T1 | `get_weather_forecast` | 城市天气预报（1.4 万+ 城市，中英文名） | 外部 API（Open-Meteo，免 Key） | ✅ |
| T2 | `fetch_webpage` | 抓取网页正文 | 外部 API | ✅ |
| T3 | `query_attractions_db` | 查本地景点库（42 城市，含币种） | 本地 SQLite | ✅ |
| T4 | `parse_budget_csv` | 解析预算 CSV | 本地文件 | ✅ |
| T5 | `convert_currency` | 汇率换算（在线 + 离线兜底） | 外部 API | ✅ |
| T6 | `estimate_route` | 城际交通估算（支持国际航线） | 本地计算（Haversine） | ✅ |
| T7 | `save_itinerary` | 计划落盘（沙箱） | 本地文件（写操作） | ❌ |
| T8 | `ask_user_clarification` | 向用户澄清 | HITL 中断 | ❌ |

所有工具返回统一信封 `{ok, data, error, meta}`，错误码分「可重试」与「确定性」两类。

---

## 数据覆盖

### 城市解析：三层策略

在线地理编码对中文城市名**不可靠**，实测未收录城市的失败率约 40%，
而且最危险的是**静默返回错误城市**：

| 查询 | 只用 Open-Meteo 的返回 |
|---|---|
| `name=开罗` | 开罗 / **美国** (37.0, −89.2) ← 美国伊利诺伊州的 Cairo |
| `name=里斯本` | 里斯本 / **美国** (46.4, −97.7) ← 美国北达科他州的 Lisbon |
| `name=东京` | 东京 / **江苏** ← 中国江苏的一个镇 |
| `name=伊斯坦布尔` `华沙` `内罗毕` | 无结果 |

这比报错更糟——错误被伪装成了事实。因此改为**本地优先**：

| 层 | 数据源 | 规模 |
|---|---|---|
| 1 | 策展表 `cities.py`（人工维护） | 170 条 |
| 2 | 生成索引 `city_index.tsv`（GeoNames） | **14,359** 个中文城市名 / 229 个国家 |
| 3 | Open-Meteo 地理编码（兜底，带 `confidence` 标记） | — |

支持这些写法：`上海市`→上海 · `达沃市`/`达沃`→达沃 · `科威特城`→科威特城 ·
`維也納`→维也纳 · `new york`/`NEW YORK`/`newyork`→纽约 · `NYC`→纽约 ·
`三藩市`→旧金山 · `乔治市`→槟城

### 景点库（42 城市 / 282 条）

**中国全部 34 个省级行政区**：

| 类型 | 城市 |
|---|---|
| 直辖市（4） | 北京、天津、上海、重庆 |
| 省省会（23） | 石家庄、太原、沈阳、长春、哈尔滨、南京、杭州、合肥、福州、南昌、济南、郑州、武汉、长沙、广州、海口、成都、贵阳、昆明、西安、兰州、西宁、台北 |
| 自治区首府（5） | 呼和浩特、南宁、拉萨、银川、乌鲁木齐 |
| 特别行政区（2） | 香港、澳门 |
| 热门非省会 | 三亚 |

**国际城市（7）**：纽约、东京、巴黎、伦敦、新加坡、曼谷、首尔。

> 香港、澳门属「中国」，台北属「中国台湾」，使用当地货币（HKD / MOP / TWD）计价。

每条景点含名称、标签、人均票价、建议时长、评分、是否亲子友好、开放时间与备注。
`open_hours` 会写清季节性限制——例如哈尔滨冰雪大世界标注「12月-2月 11:00-21:30」，
实测中 Agent 据此**主动把该项目排除出 10 月行程**。

---

## 项目结构

```
AI-Agent/
├─ README.md                      ← 你正在看的这份
├─ LICENSE                        ← MIT + GeoNames 署名
├─ docs/
│  ├─ task-planning-assistant-plan.md   项目计划书（计划清单/规则/系统提示词/测试用例）
│  └─ task-planner-test-report.md       评测报告（193 用例 + 缺陷修复记录）
└─ task-planner/                  ← 项目代码
   ├─ agent/
   │  ├─ loop.py                  编排循环 + 全部护栏（核心）
   │  ├─ events.py                事件流定义（run_stream 的产出类型）
   │  ├─ llm_client.py            DeepSeek 客户端（OpenAI 兼容）
   │  ├─ tool_runner.py           MCPToolRunner / LocalToolRunner
   │  ├─ schema.py                Plan 的 Pydantic 模型 + JSON 抽取
   │  ├─ config.py                环境变量 + base_url 校验 + 日期注入
   │  ├─ demo.py                  离线演示用的脚本化 LLM
   │  ├─ cli.py                   命令行入口
   │  └─ prompts/system.md        系统提示词
   ├─ mcp_server/
   │  ├─ server.py                MCP Server（stdio，mcp 2.x API）
   │  ├─ tools/                   8 个工具 + base.py（注册表与调用框架）
   │  └─ data/
   │     ├─ cities.py             策展城市表（170 条）
   │     ├─ city_index.tsv        生成的城市索引（14,359 条，900 KB）
   │     ├─ build_city_index.py   从 GeoNames 重建索引
   │     ├─ countries.py          国家代码 → 中文名（229 条）
   │     └─ seed.py               景点库种子数据（282 条）
   ├─ web/
   │  ├─ app.py                   FastAPI 路由 + SSE + 限流
   │  ├─ session.py               会话状态、事件重放、澄清挂起/唤醒
   │  ├─ serve.py                 启动入口（task-planner-web）
   │  └─ static/                  前端构建产物（随仓库提交，免 Node 运行）
   ├─ desktop/
   │  └─ app.py                   桌面版入口（pywebview 原生窗口）
   ├─ frontend/                   前端源码（React + TypeScript + Vite）
   │  └─ src/
   │     ├─ api/                  类型契约 + HTTP/SSE 客户端
   │     ├─ hooks/                会话状态机
   │     └─ components/           输入 / 进度 / 澄清 / 计划
   ├─ common/envelope.py          统一返回信封
   ├─ tests/                      225 个用例
   └─ outputs/                    计划落盘沙箱
```

---

## 配置

复制 `.env.example` 为 `.env`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 必填（`--demo` 除外） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | **必须填 OpenAI 兼容端点** |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `LLM_TIMEOUT` | `60` | 单次 LLM 调用超时（秒） |
| `LLM_MAX_RETRIES` | `2` | LLM 调用失败重试次数 |
| `LLM_TEMPERATURE` | `0.2` | 采样温度（0–2） |
| `MAX_TURNS` | `8` | 编排循环最大轮次 |
| `TOOL_BUDGET` | `12` | 单次会话工具调用总预算 |
| `TOOL_TIMEOUT` | `10` | 单次工具超时（秒） |
| `TOOL_RETRIES` | `1` | 工具失败重试次数 |
| `OUTPUT_DIR` | `outputs` | 落盘沙箱根目录 |
| `ATTRACTIONS_DB` | 内置路径 | 覆盖景点库位置 |
| `ALLOW_PRIVATE_URLS` | — | 设为 `1` 允许 `fetch_webpage` 访问内网地址。**仅限本地开发/测试，生产环境不要开** |

---

## 测试

```bash
uv run pytest          # 275 passed
uv run pytest -v       # 查看每个用例名
```

| 测试文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `test_cities.py` | 116 | 城市解析三层策略、景点库覆盖、**21 个错误解析回归用例** |
| `test_fixes.py` | 43 | **代码评审发现的缺陷回归**：SSRF、标题解析、计划结构校验、trace 语义 |
| `test_tools.py` | 28 | 8 个工具的行为、错误码、沙箱、重试、幂等 |
| `test_loop.py` | 27 | 编排循环 + 全部护栏 |
| `test_config.py` | 18 | base_url 校验、日期注入、**密钥泄漏扫描** |
| `test_stream.py` | 16 | 事件流序列、**JSON 可序列化**、澄清挂起与恢复 |
| `test_web.py` | 23 | HTTP 路由、**SSE 重放/续传**、澄清跨请求、**会话回收不泄漏协程** |
| `test_mcp_bridge.py` | 4 | MCP 协议桥接（真实拉起子进程） |

测试策略是**脚本化 LLM + 真实工具**：LLM 按剧本返回（不消耗 API 额度），
工具真实执行。因此测的是「编排逻辑 + 工具 + 护栏」这条完整链路，
结果确定可复现。

详见 [`docs/task-planner-test-report.md`](docs/task-planner-test-report.md)，
里面记录了 11 个开发过程中发现并修复的真实缺陷（含一个 API Key 泄漏隐患）。

---

## 设计要点

几个非显然的工程决策，值得单独说明：

**1. 为什么城市解析要本地优先？**
因为第三方地理编码的错误是**静默**的。`name=开罗` 会返回美国伊利诺伊州的 Cairo，
不报错、看起来还挺合理，然后被模型当成事实写进计划。这比抛异常危险得多。
所以引入本地索引做确定性解析，在线接口只作为带置信度标记的最后兜底。

**2. 为什么工具返回统一信封而不是裸字符串？**
`{ok, data, error, meta}` 让错误码可以被程序判定，从而区分「值得重试」
（`UPSTREAM_TIMEOUT`）和「重试也没用」（`BAD_ARGS`）。这是重试逻辑成立的前提。

**3. 为什么要有工具预算和幂等去重？**
模型会重复请求同一个调用。没有去重会浪费预算，没有预算上限会陷入循环烧 token。
命中缓存的调用不消耗预算，实测中一次 5 调用的任务里省下了 2 次。

**4. 为什么把当前日期注入系统提示词？**
LLM 不知道"今天"是哪天。不注入的话，用户说"周末去成都"，模型只能反问
"具体是哪个周末"——而这是它本该自己推算的。现在 `{{CURRENT_DATE}}`
在渲染时替换，相对时间规则写进提示词。

**5. 为什么单一 Schema 来源？**
工具的 Pydantic 模型同时生成 MCP `inputSchema` 和 LLM `tools` 参数。
两处分别定义必然漂移，而漂移导致的 bug 极难排查。有测试断言两者完全一致。

**6. 为什么把「一次性返回」改成事件流？**
一次规划要 10–60 秒，前端只给个转圈用户会以为卡死。改成 `run_stream()` 产出事件后，
可以实时显示"正在查天气…""已找到 10 个景点…"。

关键是**改法**：让 `run()` / `run_async()` 变成事件流的消费者，而不是重写一份逻辑。
这样对外行为完全不变——重构时 193 个既有测试一行没改就全绿，这是判断改造是否安全的
最好信号。另外硬性要求 `event.data` 必须 JSON 可序列化，SSE 才能原样转发。

---

## 常见问题

**`openai.NotFoundError: Error code: 404`**

99% 是 `DEEPSEEK_BASE_URL` 填错了。DeepSeek 有两个兼容端点，文档站上挨得很近：

| 端点 | 用途 | 本项目 |
|---|---|---|
| `https://api.deepseek.com` | OpenAI 兼容 | ✅ 用这个 |
| `https://api.deepseek.com/anthropic` | Anthropic 兼容（Claude Code 用） | ❌ 会拼成 `/anthropic/chat/completions` → 404 |

现在启动时就会校验并直接告诉你哪里错了。

**Agent 反问"具体是哪个周末的日期？"**

正常情况下它应该自己推算。若出现反问，检查 `agent/prompts/system.md` 里的
`{{CURRENT_DATE}}` / `{{CURRENT_WEEKDAY}}` 占位符是否被改坏——
它们由 `agent/config.py::render_system_prompt()` 在运行时替换。

**改了景点库数据但查询结果没变**

```bash
cd task-planner && uv run python -m mcp_server.data.seed
```

**想更新城市索引**

```bash
uv run python -m mcp_server.data.build_city_index --offline   # 用缓存
uv run python -m mcp_server.data.build_city_index             # 重新下载
```

---

## 已知边界

- **只做规划，不做执行** —— 不订票、不付款、不发消息。
- **景点价格为演示数据** —— 量级参考，非实时票价，未接入真实票务 API。
- **景点库覆盖 42 城** —— 超出范围会返回 NOT_FOUND 并如实告知，不会编造。
- **天气只有未来约 16 天** —— 超出会返回含可用区间的明确错误。
- **`estimate_route` 是量级估算** —— 基于直线距离折算，不是导航结果。
- **`fetch_webpage` 不执行 JS** —— 动态渲染站点可能抓不到内容。
- **汇率离线表是近似值** —— 在线失败时降级，并在返回中显式标注。

---

## 关于 AI 生成代码

本项目是 Experiment 1「Bring Your Own Agent」的作业，要求用 AI 生成样板代码。
为便于审阅，标注各部分来源：

| 类别 | 文件 |
|---|---|
| ✍️ **人工编写** | `agent/prompts/system.md`（系统提示词）<br>`agent/loop.py`（编排循环与全部护栏）<br>`agent/events.py`（事件流设计）<br>`web/session.py`（会话与澄清挂起机制）<br>`tests/`（测试用例来自需求设计）<br>`mcp_server/data/seed.py`（景点数据）<br>`mcp_server/data/cities.py`（策展城市表） |
| 🤖 AI 生成后人工复核 | `mcp_server/tools/*.py`（8 个工具）<br>`mcp_server/server.py`、`agent/llm_client.py`<br>`agent/tool_runner.py`、`agent/schema.py`<br>`agent/cli.py`、`agent/config.py`、`agent/demo.py`<br>`mcp_server/data/build_city_index.py`<br>`web/app.py`、`web/serve.py`<br>`frontend/`（React 组件、状态机、样式） |

---

## 许可与数据来源

[MIT License](LICENSE)

- 城市索引派生自 [GeoNames](https://www.geonames.org/)，遵循
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- 天气数据由 [Open-Meteo](https://open-meteo.com/) 提供
- 汇率数据由 [open.er-api.com](https://open.er-api.com/) 提供
