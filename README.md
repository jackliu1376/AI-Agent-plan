# 任务规划助手 · Task Planner

> 把一句"我想周末去成都玩两天"变成一份**有序、可执行、带依赖关系**的行动计划。
> 一个基于 **MCP 协议**的工具调用 Agent，会自己查天气、翻景点库、算交通，缺数据时如实说而不是编。

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-193%20passed-2ea44f)
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

### 其他命令

```bash
uv run task-planner --list-tools                    # 查看已注册工具
uv run task-planner --demo --save chengdu.md        # 保存计划到 outputs/
uv run python -m mcp_server.server --list           # 打印 MCP 暴露的 Schema
```

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
   ├─ common/envelope.py          统一返回信封
   ├─ tests/                      193 个用例
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
| `MAX_TURNS` | `8` | 编排循环最大轮次 |
| `TOOL_BUDGET` | `12` | 单次会话工具调用总预算 |
| `TOOL_TIMEOUT` | `10` | 单次工具超时（秒） |
| `TOOL_RETRIES` | `1` | 工具失败重试次数 |
| `OUTPUT_DIR` | `outputs` | 落盘沙箱根目录 |
| `ATTRACTIONS_DB` | 内置路径 | 覆盖景点库位置 |

---

## 测试

```bash
uv run pytest          # 193 passed
uv run pytest -v       # 查看每个用例名
```

| 测试文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `test_cities.py` | 116 | 城市解析三层策略、景点库覆盖、**21 个错误解析回归用例** |
| `test_tools.py` | 28 | 8 个工具的行为、错误码、沙箱、重试、幂等 |
| `test_loop.py` | 27 | 编排循环 + 全部护栏 |
| `test_config.py` | 18 | base_url 校验、日期注入、**密钥泄漏扫描** |
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
| ✍️ **人工编写** | `agent/prompts/system.md`（系统提示词）<br>`agent/loop.py`（编排循环与全部护栏）<br>`tests/`（测试用例来自需求设计）<br>`mcp_server/data/seed.py`（景点数据）<br>`mcp_server/data/cities.py`（策展城市表） |
| 🤖 AI 生成后人工复核 | `mcp_server/tools/*.py`（8 个工具）<br>`mcp_server/server.py`、`agent/llm_client.py`<br>`agent/tool_runner.py`、`agent/schema.py`<br>`agent/cli.py`、`agent/config.py`、`agent/demo.py`<br>`mcp_server/data/build_city_index.py` |

---

## 许可与数据来源

[MIT License](LICENSE)

- 城市索引派生自 [GeoNames](https://www.geonames.org/)，遵循
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- 天气数据由 [Open-Meteo](https://open-meteo.com/) 提供
- 汇率数据由 [open.er-api.com](https://open.er-api.com/) 提供
