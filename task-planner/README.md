# 任务规划助手（Task Planner）

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

### 1. Tool Use / Skills —— 8 个功能互异的技能

| ID | 工具 | 功能 | 类型 | 幂等 |
|---|---|---|---|---|
| T1 | `get_weather_forecast` | 城市天气预报（**中英文名均可，支持国际城市**） | **外部 API**（Open-Meteo，免 Key） | ✅ |
| T2 | `fetch_webpage` | 抓取网页正文 | **外部 API** | ✅ |
| T3 | `query_attractions_db` | 查本地景点库（**12 城市，含币种**） | **本地 SQLite** | ✅ |
| T4 | `parse_budget_csv` | 解析预算 CSV | **本地文件** | ✅ |
| T5 | `convert_currency` | 汇率换算 | 外部 API + 离线兜底 | ✅ |
| T6 | `estimate_route` | 城际交通估算（**支持国际航线**） | **本地计算**（Haversine） | ✅ |
| T7 | `save_itinerary` | 计划落盘 | 本地文件（写操作，沙箱） | ❌ |
| T8 | `ask_user_clarification` | 向用户澄清 | HITL 中断 | ❌ |

最低达标只需 T1 + T3（外部 API + 本地数据库），本项目给了 8 个。

### 城市覆盖（共 55 个）

| 国家/地区 | 数量 | 城市 |
|---|---|---|
| 中国 | 25 | 北京、上海、广州、深圳、成都、重庆、杭州、南京、西安、武汉、长沙、青岛、厦门、三亚、海口、昆明、哈尔滨、沈阳、郑州、兰州、拉萨、乌鲁木齐、桂林、丽江、香港 |
| 美国 | 4 | 纽约、洛杉矶、旧金山、拉斯维加斯 |
| 日本 | 4 | 东京、大阪、京都、札幌 |
| 澳大利亚 | 2 | 悉尼、墨尔本 |
| 加拿大 | 2 | 温哥华、多伦多 |
| 泰国 | 2 | 曼谷、普吉 |
| 越南 | 2 | 河内、胡志明市 |
| 韩国 | 1 | 首尔 |
| 新加坡 | 1 | 新加坡 |
| 马来西亚 | 1 | 吉隆坡 |
| 印度尼西亚 | 1 | 巴厘岛 |
| 阿联酋 | 1 | 迪拜 |
| 法国 | 1 | 巴黎 |
| 英国 | 1 | 伦敦 |
| 意大利 | 1 | 罗马 |
| 西班牙 | 1 | 巴塞罗那 |
| 荷兰 | 1 | 阿姆斯特丹 |
| 德国 | 1 | 柏林 |
| 瑞士 | 1 | 苏黎世 |
| 俄罗斯 | 1 | 莫斯科 |
| 新西兰 | 1 | 奥克兰 |

**天气与交通**覆盖上表全部 55 个城市；**景点库**覆盖其中 12 个：
成都、杭州、北京、西安、三亚、纽约、东京、巴黎、伦敦、新加坡、曼谷、首尔。

> 城市名解析采用「**本地坐标表优先，未命中才回落 Open-Meteo 地理编码**」。
> 这是必要的：Open-Meteo 的地理编码对中文城市名基本不可用，且会**静默返回错误结果** ——
> `name=东京` 会命中「江苏·东京」(32.21,119.29)，`name=New York` 会命中
> 「内布拉斯加州·约克」。本地表命中时直接使用确定坐标；回落时则返回
> `resolved.country` 与 `confidence` 供模型复核，低置信度结果会附警告。

### 外币价格

景点库的价格是**当地货币**（`currency` 字段标注）。Agent 会用 `convert_currency`
把外币折算成人民币，例如实测中它自行把 `197 USD` 换算成 `¥1,324` 再算预算。


### 2. Context Integration（MCP）—— 桥接 Agent 大脑与本地环境

```
User → Orchestrator(编排循环) → DeepSeek(函数调用决策)
     → MCPToolRunner(Client) ──stdio/JSON-RPC──→ MCPServer → 8 个工具 → 外部API / SQLite / 文件
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
| `mcp_server/tools/*.py`（8 个工具） | 🤖 AI 生成 | 样板式实现，人工复核过参数校验与错误码 |
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
│     ├─ seed.py         景点库种子数据（35 条，5 个城市）
│     ├─ attractions.db  自动生成
│     └─ sample_budget.csv
├─ common/envelope.py    统一返回信封 {ok, data, error, meta}
├─ tests/                TC-01 ~ TC-10
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

10 个用例对齐需求文档中的 TC-01 ~ TC-10，覆盖正常路径、澄清门控、
多工具协作、工具失败降级、不可行约束、依赖 DAG 无环、跨领域泛化、提示注入防御。

---

## 已知边界

- 只做**规划**，不做执行（不订票、不付款、不发消息）。
- 天气依赖 Open-Meteo，只提供未来约 16 天预报；超出范围会返回明确错误而非编造。
- `estimate_route` 基于直线距离折算，是量级估算，不是导航结果。
- `fetch_webpage` 只抓正文文本，不执行 JS，动态渲染站点可能抓不到内容。
- 汇率优先在线，失败时降级到内置离线表并显式标注。
- 景点库是离线种子数据（39 条 / 5 个城市），用于演示，不代表实时信息。
  改了 `mcp_server/data/seed.py` 后运行 `uv run python -m mcp_server.data.seed` 重建；
  或设置 `ATTRACTIONS_DB` 指向自己的库（此时不会被自动覆盖）。

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
100 passed in 53.51s     # 通过率 100%
```

其中包含两条**密钥泄漏防护**测试：`.env.example` 只允许放占位符，
且全项目扫描确保除 `.env` 外没有任何文件含真实 Key。
明细、缺陷修复记录与真实 DeepSeek 端到端验证见 `../docs/task-planner-test-report.md`。
