# Cairn · 任务规划助手

> 把复杂任务（如「我想去旅行」）自动分解为**有序、可执行、带依赖关系**的具体步骤。
> 对应 Experiment 1「Bring Your Own Agent (BYOA)」的三条硬性要求。

---

## 它做什么

输入一段自然语言任务描述 → Agent 调用工具取证 → 输出一份结构化计划：

```
阶段(phase) → 步骤(step) → 动作(action) → 工具(tool) → 前置依赖(depends_on)
           → 预估耗时(eta) → 产出物(deliverable) → 验收标准(done_when)
```

输出同时提供 **Markdown 人类可读版** 与 **JSON 机器可解析版**。

---

## 快速开始

```bash
# 1. 安装依赖（需要 uv）
uv sync

# 2. 无 API Key 也能看完整效果（脚本化 LLM，工具真实执行）
uv run task-planner --demo

# 3. 接入真实 DeepSeek
cp .env.example .env      # 填入 DEEPSEEK_API_KEY
uv run task-planner "我想周末去成都玩两天，带小孩，人均预算 1000" -v

# 4. 走 MCP 协议调用工具
uv run task-planner "帮我规划杭州 3 日游" --transport mcp -v

# 5. 保存计划到 outputs/
uv run task-planner --demo --save chengdu-2d.md

# 6. 跑测试
uv run pytest
```

---

## 三条硬性要求如何满足

### 1. Tool Use / Skills —— 10 个功能互异的技能

| ID | 工具 | 功能 | 类型 | 幂等 |
|---|---|---|---|---|
| T1 | `get_weather_forecast` | 城市天气预报（**1.4 万+ 城市，中英文名**） | **外部 API**（Open-Meteo，免 Key） | ✅ |
| T2 | `fetch_webpage` | 抓取网页正文 | **外部 API** | ✅ |
| T3 | `query_attractions_db` | 查本地景点库（**305 城市，含币种**） | **本地 SQLite** | ✅ |
| T4 | `parse_budget_csv` | 解析预算 CSV | **本地文件** | ✅ |
| T5 | `convert_currency` | 汇率换算 | 外部 API + 离线兜底 | ✅ |
| T6 | `estimate_route` | 城际交通估算（**支持国际航线**） | **本地计算**（Haversine） | ✅ |
| T7 | `save_itinerary` | 计划落盘 | 本地文件（写操作，沙箱） | ❌ |
| T8 | `ask_user_clarification` | 向用户澄清 | HITL 中断 | ❌ |
| T9 | `query_transit_options` | 查普速列车班次（含跨天到站） | 本地数据 | ✅ |
| T10 | `query_attraction_realtime` | 查景点的**实时**开放状态 | 外部 API | ✅ |

最低达标只需 T1 + T3（外部 API + 本地数据库），本项目给了 10 个。

### 城市解析：三层策略

在线地理编码对中文城市名**不可靠**，实测未收录城市的失败率约 40%，而且最危险的是
**静默返回错误城市**：

| 查询 | Open-Meteo 单独使用时的返回 |
|---|---|
| `name=开罗` | 开罗 / **美国** (37.0, −89.2) ← 美国伊利诺伊州的 Cairo |
| `name=里斯本` | 里斯本 / **美国** (46.4, −97.7) ← 美国北达科他州的 Lisbon |
| `name=米兰` | 米兰 / **美国** |
| `name=伊斯坦布尔` `华沙` `内罗毕` `加德满都` | 无结果 |

这比报错更糟——错误被伪装成了事实。因此改为**本地优先**：

| 层 | 数据源 | 规模 | 说明 |
|---|---|---|---|
| 1 | **策展表** `cities.py` | 250 条 | 人工维护。含**非城市目的地**（圣托里尼、长滩岛、少女峰、马丘比丘…）与译名差异（科伦坡 vs GeoNames 的「可倫坡」）。元数据最全：中文国家名、时区、别名 |
| 2 | **生成索引** `city_index.tsv` | **14,359** 个中文城市名 | 由 `build_city_index.py` 从 GeoNames 生成，覆盖 **229 个国家/地区**。零网络、确定性 |
| 3 | **在线地理编码** Open-Meteo | — | 最后兜底。拉 10 个候选按「行政级别 + 人口」排序，返回 `confidence` 标记供模型复核 |

查询时还会做归一化，因此这些写法都能命中：

- 行政区后缀：`上海市` → 上海、`达沃市` / `达沃` → 达沃、`科威特城` → 科威特城
- 繁体中文：`維也納` → 维也纳、`米蘭` → 米兰、`伊斯坦堡` → 伊斯坦布尔
- 大小写 / 空格：`new york` / `NEW YORK` / `newyork` → 纽约
- 别名：`NYC` → 纽约、`三藩市` → 旧金山、`乔治市` → 槟城、`西贡` → 胡志明市

### 景点库覆盖（305 个城市 / 6,103 条景点）

**中国全部 34 个省级行政区**都已覆盖：

| 类型 | 城市 |
|---|---|
| 直辖市（4） | 北京、天津、上海、重庆 |
| 省省会（23） | 石家庄、太原、沈阳、长春、哈尔滨、南京、杭州、合肥、福州、南昌、济南、郑州、武汉、长沙、广州、海口、成都、贵阳、昆明、西安、兰州、西宁、**台北** |
| 自治区首府（5） | 呼和浩特、南宁、拉萨、银川、乌鲁木齐 |
| 特别行政区（2） | **香港**、**澳门** |
| 热门非省会 | 三亚 |

**国际城市（7 个）**：纽约、东京、巴黎、伦敦、新加坡、曼谷、首尔。

> 港澳台标注：香港、澳门属「中国」，台北属「中国台湾」，使用当地货币
> （HKD / MOP / TWD）计价。

每个城市至少 5 个景点，包含名称、标签、人均票价、建议游玩时长、评分、
是否亲子友好、开放时间与备注。`open_hours` 会写清季节性限制，
例如哈尔滨冰雪大世界标注「12月-2月 11:00-21:30」——
实测中 Agent 据此**主动把该项目排除出 10 月行程**。

### 外币价格

景点库的价格是**当地货币**（`currency` 字段标注）。Agent 会用 `convert_currency`
把外币折算成人民币，例如实测中它自行把 `197 USD` 换算成 `¥1,324` 再算预算。
离线汇率表覆盖 CNY/USD/EUR/JPY/KRW/HKD/TWD/MOP/GBP/THB/SGD/MYR/AUD。

### 重建城市索引

索引已随仓库提交（900 KB），clone 即可用。需要更新时：

```bash
uv run python -m mcp_server.data.build_city_index            # 下载并重建
uv run python -m mcp_server.data.build_city_index --offline  # 用已有缓存
```

> 数据来源：[GeoNames](https://www.geonames.org/)（CC BY 4.0）。原始数据包缓存在
> `mcp_server/data/.cache/`，已加入 `.gitignore`。

### 重建景点库

改了 `mcp_server/data/seed.py` 后运行：

```bash
uv run python -m mcp_server.data.seed
```

正常情况下 `connect()` 会检测**列结构与行数**变化并自动重建，
但如果你设置了 `ATTRACTIONS_DB` 指向自己的库，就不会被自动覆盖。




### 2. Context Integration（MCP）—— 桥接 Agent 大脑与本地环境

```
User → Orchestrator(编排循环) → DeepSeek(函数调用决策)
     → MCPToolRunner(Client) ──stdio/JSON-RPC──→ MCPServer → 10 个工具 → 外部API / SQLite / 文件
```

- **MCP Server**：`mcp_server/server.py`，用低层 `Server` 的 `on_list_tools` / `on_call_tool` 回调暴露工具，Schema 直接来自注册表（适配 `mcp >= 2.2`；2.x 中 FastMCP 已更名为 MCPServer）。
- **MCP Client**：`agent/tool_runner.py::MCPToolRunner`，走官方 `mcp` SDK 的 stdio 传输。
- **降级通道**：`LocalToolRunner` 进程内直调同一批函数，用于单元测试与 MCP 不可用时的兜底。
- **单一 Schema 来源**：`TOOL_REGISTRY` 里的 Pydantic 模型同时生成 MCP `inputSchema` 与 LLM `tools` 参数，避免两处漂移。

验证 MCP 桥接：

```bash
uv run python -m mcp_server.server --list    # 打印 MCP 暴露的工具 Schema
uv run task-planner "去三亚玩 4 天" --transport mcp -v
```

### 3. Vibe Coding 留痕

| 文件 | 来源 | 说明 |
|---|---|---|
| `mcp_server/tools/base.py` | 🤖 AI 生成 | 注册表 + 重试/计时/错误映射框架 |
| `mcp_server/tools/*.py`（10 个工具） | 🤖 AI 生成 | 样板式实现，人工复核过参数校验与错误码 |
| `mcp_server/server.py` | 🤖 AI 生成 | MCP Server 骨架 |
| `agent/llm_client.py` | 🤖 AI 生成 | OpenAI 兼容客户端样板 |
| `agent/tool_runner.py` | 🤖 AI 生成 | MCP / Local 双通道 |
| `agent/schema.py` | 🤖 AI 生成 | Pydantic 模型样板 |
| `agent/cli.py` | 🤖 AI 生成 | argparse 样板 |
| **`agent/prompts/system.md`** | ✍️ **人工编写** | 系统提示词（评分核心） |
| **`agent/loop.py`** | ✍️ **人工编写 + AI 辅助** | 编排循环与全部护栏（评分核心） |
| **`tests/`** | ✍️ **人工设计** | 测试用例来自需求，不是 AI 编的 |
| `agent/config.py` `agent/demo.py` | 🤖 AI 生成 | 配置与演示脚本 |

---

## 架构

```
┌──────────────┐
│  用户输入     │  复杂任务描述
└──────┬───────┘
       ▼
┌──────────────────────────────────┐        ┌──────────────┐
│ 编排循环 Orchestrator             │◄──────►│  DeepSeek    │
│ 解析→澄清→取证→分解→排序→校验→输出 │  函数调用 │ deepseek-chat │
│ 护栏：轮次≤8 预算≤12 去重 重试降级  │        └──────────────┘
└──────┬───────────────────────────┘
       ▼
┌──────────────────────────────────┐
│ MCP 桥接层（stdio / JSON-RPC）     │
│ MCPToolRunner ──► MCPServer      │
└──────┬───────────────────────────┘
       ▼
┌──────────┬──────────┬──────────┬──────────┐
│ 天气 API  │ 网页抓取  │ 本地SQLite│ CSV 解析  │
└──────────┴──────────┴──────────┴──────────┘
```

---

## 项目结构

```
task-planner/
├─ agent/
│  ├─ loop.py            编排循环 + 全部护栏（核心）
│  ├─ llm_client.py      DeepSeek 客户端（OpenAI 兼容）
│  ├─ tool_runner.py     MCPToolRunner / LocalToolRunner
│  ├─ schema.py          Plan 的 Pydantic 模型 + JSON 抽取
│  ├─ config.py          环境变量配置
│  ├─ demo.py            离线演示用脚本化 LLM
│  ├─ cli.py             命令行入口
│  └─ prompts/system.md  系统提示词
├─ mcp_server/
│  ├─ server.py          MCP Server（stdio）
│  ├─ tools/
│  │  ├─ base.py         注册表 + 调用框架
│  │  ├─ weather.py  web.py  attractions.py  budget.py
│  │  └─ currency.py route.py storage.py clarify.py
│  └─ data/
│     ├─ cities.py          策展城市表（250 条，含非城市目的地）
│     ├─ countries.py       国家代码 → 中文名
│     ├─ city_index.tsv     生成的城市索引（14,359 个中文名，900 KB）
│     ├─ build_city_index.py 从 GeoNames 重建索引（构建期脚本）
│     ├─ seed.py            景点库种子数据（6,103 条，305 个城市）
│     ├─ attractions.db     自动生成
│     └─ sample_budget.csv
├─ common/envelope.py    统一返回信封 {ok, data, error, meta}
├─ tests/                TC-01 ~ TC-10 + 城市解析回归（675 项）
└─ outputs/              计划落盘沙箱
```

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 必填（`--demo` 模式除外） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `MAX_TURNS` | `8` | 编排循环最大轮次 |
| `TOOL_BUDGET` | `12` | 单次会话工具调用总预算 |
| `TOOL_TIMEOUT` | `10` | 单次工具超时（秒） |
| `TOOL_RETRIES` | `1` | 工具失败重试次数 |
| `OUTPUT_DIR` | `outputs` | 落盘沙箱根目录 |
| `ATTRACTIONS_DB` | 内置路径 | 覆盖景点库位置 |

---

## 测试

```bash
uv run pytest -v
```

675 项测试对齐需求文档中的 TC-01 ~ TC-10，覆盖正常路径、澄清门控、
多工具协作、工具失败降级、不可行约束、依赖 DAG 无环、跨领域泛化、提示注入防御。

---

## 已知边界

- 只做**规划**，不做执行（不订票、不付款、不发消息）。
- 天气依赖 Open-Meteo，只提供未来约 16 天预报；超出范围会返回明确错误而非编造。
- `estimate_route` 基于直线距离折算，是量级估算，不是导航结果。
- `fetch_webpage` 只抓正文文本，不执行 JS，动态渲染站点可能抓不到内容。
- 汇率优先在线，失败时降级到内置离线表并显式标注。
- 景点库是离线种子数据（6,103 条 / 305 个城市），价格为量级参考，不代表实时票价。
  详见上方「重建景点库」。

## 安全约定

- 真实密钥只写在 `.env`（已被 `.gitignore` 忽略）。
- **`.env.example` 只放占位符**，因为它是给 git 提交用的模板。
- 有两条测试在守这条线：`test_env_example_uses_placeholder_only`
  与 `test_no_real_api_key_in_committable_files`（全项目扫描）。
  跑 `uv run pytest` 就会拦截误提交。

## 常见问题

**`openai.NotFoundError: Error code: 404`**

99% 是 `DEEPSEEK_BASE_URL` 填错了。DeepSeek 同时提供两个兼容端点，文档站上挨得很近：

| 端点 | 用途 | 本项目是否可用 |
|---|---|---|
| `https://api.deepseek.com` | **OpenAI 兼容** | ✅ 用这个 |
| `https://api.deepseek.com/anthropic` | Anthropic 兼容（Claude Code 等） | ❌ 会拼成 `/anthropic/chat/completions` → 404 |

现在启动时就会校验并直接告诉你哪里错了，不会再让你对着 404 猜。

**Agent 反问"具体是哪个周末的日期？"**

系统提示词会注入当天日期，正常情况下它能自己推算"周末 / 下周三 / 下个月"。
如果你看到它反问日期，说明 `agent/prompts/system.md` 里的
`{{CURRENT_DATE}}` / `{{CURRENT_WEEKDAY}}` 占位符被改坏了——
这两个占位符由 `agent/config.py::render_system_prompt()` 在运行时替换。

**改了景点库数据但查询结果没变**

运行 `uv run python -m mcp_server.data.seed` 重建数据库。
（正常情况下 `connect()` 会检测种子行数变化并自动重建；但如果你设置了
`ATTRACTIONS_DB` 指向自己的库，就不会被自动覆盖。）

## 测试通过率

```
uv run pytest
675 passed                # 通过率 100%
```

其中包含两条**密钥泄漏防护**测试：`.env.example` 只允许放占位符，
且全项目扫描确保除 `.env` 外没有任何文件含真实 Key。
明细、缺陷修复记录与真实 DeepSeek 端到端验证见 `../docs/task-planner-test-report.md`。
