# Cairn · 任务规划助手 —— 项目复盘与代码评审报告

> 评审对象：`D:\AI-Agent\task-planner`  
> 评审日期：2026-10-08  
> 评审方式：全量源码阅读 + 实测验证（不依赖注释与文档自述）  
> 代码规模：Python 约 17,500 行（其中 5,862 行为生成式种子数据，真实手写代码约 11,000 行）；前端 TypeScript 约 4,000 行；测试 8,626 行  
> 当前状态：**测试 675 项全部通过**（`pytest` EXIT=0），前端 `tsc` 类型检查通过

---

## 修复进展（滚动更新）

| 批次 | 范围 | 状态 |
|---|---|---|
| **P0** | T01–T07（真 bug，7 项） | ✅ **7/7 完成** |
| **P1** | T08–T14（竞态 + 可访问性 + 配置告警，7 项） | ✅ **7/7 完成** |
| **P2** | T15–T21（性能 + 资源，7 项） | ✅ **6/7 完成**，1 项实测后判定不改 |
| **P6** | T40（高德覆盖范围，**用户报障**） | ✅ **完成**（见 2.1 节 S7） |
| P3 | T27（同步 README 过时数字） | ✅ 完成 |
| P4 / P5 | 共 12 项 | ⏳ 待做 |

**本轮修复涉及的文件**：

| 文件 | 改动 |
|---|---|
| `mcp_server/tools/route.py` | 拆 `HSR_MODES` 修普速参数 bug；**新增 `amap_covers()` 覆盖范围判定**；`Any` 导入；Haversine clamp |
| `mcp_server/tools/attraction_live.py` | **调用前判掉境外**；`Any` 导入 |
| `mcp_server/tools/transit.py` | **调用前判掉境外**；修正误导性失败原因 |
| `mcp_server/tools/attractions.py` | **区分「城市未收录」与「筛选筛空」** |
| `mcp_server/server.py` | `on_call_tool` 异步化；工具数动态化 |
| `mcp_server/data/seed.py` | `connect()` 改上下文管理器（退出即关闭） |
| `mcp_server/tools/weather.py` | 新增 `_upstream_failure()`；两处网络异常兜底 |
| `agent/config.py` | 新增 `_warn_fallback()`；非法值告警 |
| `agent/llm_client.py` | 新增 `close()` 释放连接池 |
| `agent/run_log.py` | `load_runs` 改逐行流式读取 |
| `web/session.py` | `_persist` 异步化 + `RLock` 互斥；`stream_done` 判据；按轮重置运行标记 |
| `web/session_db.py` | `_ensure_schema` 加锁；`_prune` 条件化（**WAL 实测有害，未采纳**） |
| `web/app.py` | 初版/修订合并为 `_drive`；终态事件扣到落盘后；`RateLimiter.prune()`；`_close_run()` |
| `pyproject.toml` | wheel 补 `web`、`desktop` |
| `frontend/src/api/client.ts` | SSE 重连防重入；`readyState === CLOSED` 快速失败 |
| `frontend/src/hooks/usePlanningSession.ts` | `submittingRef` 守卫；`requestSeqRef` 竞态仲裁；`showVersion` 不再改 `phase`；`notify()` |
| `frontend/src/components/Composer.tsx` | 版本菜单键盘可达 |
| `frontend/src/components/ShareButton.tsx` | 分享菜单键盘可达 |
| `frontend/src/components/EvidenceList.tsx` | `probes` 由调用方传入，不再各自重算 |
| `frontend/src/components/PlanView.tsx` | 加 `React.memo` |
| `frontend/src/App.tsx` | 派生值 `useMemo`；`copyMarkdown` 带失败提示 |
| `tests/` | 新增 21 条回归测试（普速参数、网络异常、并发落盘、覆盖范围等） |
| `README.md` | 同步过时数字（工具数 / 城市数 / 景点数 / 测试数） |

**评审过程中自我更正 3 处**：

1. 原「严重 S6（天气工具降级失效）」经复核为误判 → 降级为「中等 M14」（见 2.2 节）
2. 原「建议启用 WAL」经实测为有害 → **撤回建议**（见 2.2 节 M3）
3. 原「事件数组 O(n²) 需优化」经实测为 0.76ms → **判定不改**（见 2.2 节 M6）

**方法论备注**：每条修复都做了「验证测试本身有效」这一步 —— 例如 T08 的防重复提交，先确认加守卫后是 1 次 POST，再**临时移除守卫确认它变回 2 次**；T01、T40 同理。**不做反向验证，你分不清「修好了」和「测试写错了」。**

---

## 一、项目复盘总结

### 1.1 业务目标与核心问题

**一句话**：把一段自然语言的任务描述（「我想周末去成都玩两天，带小孩，人均预算 1000」），自动拆解成一份**有序、可执行、带依赖关系**的结构化计划。

它解决的不是「生成一段好看的攻略文案」，而是三个更硬的问题：

| 核心问题        | 具体表现                          | 项目的应对                                                      |
| ----------- | ----------------------------- | ---------------------------------------------------------- |
| **事实不能编**   | LLM 会凭记忆编造票价、天气、开放时间          | 强制工具取证 + `data_freshness` 字段（schema 级条件校验，非提示词约束）          |
| **输出必须可执行** | 自由文本无法被下游程序消费                 | `Phase → Step → depends_on` 的 DAG，`check_dag()` 校验无环、无悬空引用 |
| **过程必须可控**  | 单次 LLM 调用可能死循环、烧光 token、被注入带偏 | 轮次上限、工具预算、幂等去重、输入隔离定界符、注入特征检测                              |

对应 Experiment 1「Bring Your Own Agent (BYOA)」的三条硬性要求：**Tool Use / Skills**、**Context Integration (MCP)**、**Vibe Coding 留痕**。

### 1.2 整体架构

```
用户输入
   │
   ▼
┌──────────────────────────────────────────────┐
│ 编排循环 Orchestrator  (agent/loop.py)         │
│ 状态机：解析→澄清门控→取证→分解→排序→校验→输出  │
│ 护栏：轮次≤8 · 预算≤12 · 幂等去重 · 失败降级     │
│       · 澄清中断 · 注入隔离 · 修复重试(1次)      │
└───────┬──────────────────────────┬───────────┘
        │ 函数调用（OpenAI 兼容）      │ 事件流（async generator）
        ▼                          ▼
┌───────────────┐          ┌──────────────────────┐
│ LLM 客户端     │          │ Event 流              │
│ llm_client.py │          │ run_started/turn_.../ │
│ DeepSeekClient│          │ tool_call/tool_result │
│ （可换任意     │          │ /clarification/repair │
│  OpenAI 兼容） │          │ /plan_ready/error     │
└───────────────┘          └──────┬───────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
┌───────────────┐        ┌───────────────┐        ┌───────────────┐
│ CLI           │        │ Web (SSE)     │        │ Desktop       │
│ agent/cli.py  │        │ web/app.py    │        │ pywebview     │
│ ask_user=input│        │ ask_user=     │        │ 复用 Web 层   │
│               │        │  Future 挂起   │        │               │
└───────────────┘        └───────┬───────┘        └───────────────┘
                                 ▼
                    ┌────────────────────────┐
                    │ ToolRunner 双通道       │
                    │  LocalToolRunner（进程内）│
                    │  MCPToolRunner（stdio）  │
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │ MCP Server (10 个工具)  │
                    │ 天气/网页/景点库/预算CSV/ │
                    │ 汇率/交通/落盘/澄清/实时  │
                    └────────────────────────┘
```

**关键架构决策**（都有明确的取舍理由，写在代码注释里）：

1. **`run_stream()` 是唯一实现完整逻辑的入口**，`run()` / `run_async()` 只是它的消费者。CLI / Web / Desktop 三端共用同一条管道，**没有任何一处重复编排逻辑**。
2. **事件流是 JSON 可序列化的**（`agent/events.py` 明确定义为约束），所以 SSE 层不需要为每种事件写转换代码，前端也不需要后端专有协议。
3. **工具 Schema 单一来源**：`TOOL_REGISTRY` 里的 Pydantic 模型同时生成 MCP `inputSchema` 与 LLM `tools` 参数，避免两处漂移。
4. **`Plan` 是评测基准 schema**，修订功能要传的额外字段（`revision_summary`）走 JSON 旁路 key，不污染 schema —— 因为往 `Plan` 加字段会让已有评测样本全部失效。

### 1.3 数据流

**一次规划的完整链路**：

```
1. 提交任务
   POST /api/sessions {task}
   → SessionStore.create() 生成 16 位 hex session_id
   → 立刻落盘一行（防「刚提交就崩」）
   → asyncio.create_task(_drive_session) 后台跑

2. 编排（Orchestrator.run_stream）
   messages = [system_prompt(注入当天日期), *history, user(包进 <user_task>)]
   ↓ 循环 ≤8 轮
   LLM.chat(messages, tools=10个工具schema)   ← asyncio.to_thread，不阻塞事件循环
   ├─ 无 tool_calls → final_text，break
   └─ 有 tool_calls → 逐个执行
        ├─ 幂等缓存命中？→ 不消耗预算，直接返回
        ├─ 预算耗尽？→ 返回错误信封，模型必须收尾
        ├─ needs_user_input？→ 产出 clarification 事件 + 挂起等待
        └─ 执行 → 产出 tool_result 事件（含人话摘要）→ 回灌 messages

3. 输出校验
   parse_plan(final_text) → Pydantic 校验
   ├─ 失败 → 追加一次纠错指令 → 重试一次
   └─ 成功 → plan.check_dag() 校验无环

4. 收尾
   output_status = ok / invalid_output / invalid_dag / runtime_error
   PlanResult(ok = plan非空 且 dag_ok)
   → record_version() 登记版本
   → record_run() 写 logs/runs.jsonl（评测用）
   → 状态跃迁时写 logs/sessions.db（界面「最近」用）

5. 前端
   EventSource 订阅 /api/sessions/{id}/events?cursor=N
   逐事件渲染：阶段条 / 已查证列表 / 骨架屏 / 计划正文
```

**存储三层分工**（设计清晰，值得保留）：

| 层    | 位置                       | 用途           | 写入时机             |
| ---- | ------------------------ | ------------ | ---------------- |
| 内存   | `SessionStore._sessions` | 事件推送、澄清挂起    | 每个事件             |
| 会话库  | `logs/sessions.db`       | 界面「最近」、重启后打开 | **仅状态跃迁**（避免写放大） |
| 运行日志 | `logs/runs.jsonl`        | 评测统计、跨模型对比   | 每次运行一行           |

### 1.4 技术栈与关键算法

| 层次   | 选型                                 | 备注                                                |
| ---- | ---------------------------------- | ------------------------------------------------- |
| LLM  | OpenAI SDK（`base_url` 可换）          | **无任何 DeepSeek 私有特性**，已验证 MiMo / GLM 可直连          |
| 协议   | MCP ≥2.2（stdio / JSON-RPC）         | 低层 `Server` + `on_list_tools` / `on_call_tool` 回调 |
| 数据校验 | Pydantic v2                        | `Plan` 模型 + 两个 `model_validator`                  |
| Web  | FastAPI + SSE（`StreamingResponse`） | 会话落盘用标准库 `sqlite3`                                |
| 前端   | React 18 + TypeScript 5.7 + Vite 6 | 零 UI 库，全部手写 CSS                                   |
| 桌面   | pywebview                          | 复用 Web 层，不复制业务逻辑                                  |
| 数据   | SQLite（景点库）+ GeoNames 索引（TSV）      | 14,359 个中文城市名，229 个国家/地区                          |

**关键算法**：

| 算法               | 位置                                         | 要点                                                                                                     |
| ---------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| **城市解析三层策略**     | `mcp_server/data/cities.py` + `weather.py` | 策展表(170) → 生成索引(14,359) → 在线地理编码。**本地优先**，因为在线地理编码对中文名会**静默返回错误城市**（`name=开罗` → 美国伊利诺伊州 Cairo），这比报错更危险 |
| **迭代式 DFS 环检测**  | `schema.py:196-223`                        | 三色标记，用显式栈而非递归，>1000 步依赖链不触发 `RecursionError`                                                           |
| **幂等去重**         | `loop.py:600-617`                          | `(工具名, 排序后的参数JSON)` 做 key。**缓存查找排在预算门控之前**——命中缓存不消耗预算，否则预算耗尽时模型会误以为「数据缺失」                              |
| **SSRF 防护**      | `web.py:74-164`                            | 主机名黑名单 → DNS 解析后校验 IP → 手动逐跳跟随重定向并重新校验 → 流式读取限 5MB                                                     |
| **Haversine 距离** | `route.py:286-292`                         | 直线距离 × 里程系数 × 分档时速/单价                                                                                  |
| **注入特征检测**       | `loop.py:56-72`                            | 6 条正则，**仅标记与统计，不做拦截**（拦截由系统提示词负责）                                                                      |

### 1.5 能力清单与实现边界

**已实现且经过验证**

| 能力                                    | 状态   | 验证方式                                  |
| ------------------------------------- | ---- | ------------------------------------- |
| 结构化计划生成（DAG + 验收标准）                   | ✅ 完整 | 654 项测试 + 真实 DeepSeek 端到端             |
| 10 个工具（天气/网页/景点库/预算/汇率/交通/落盘/澄清/实时景点） | ✅ 完整 | 单测 + MCP 桥接测试                         |
| 澄清门控（HITL 中断 + 跨请求挂起恢复）               | ✅ 完整 | `test_revision.py` / `test_stream.py` |
| 增量修订（保持 id、级联更新、`revision_summary`）   | ✅ 完整 | `test_revision.py` 593 行              |
| SSE 事件流 + 断线重连续传（cursor）              | ✅ 完整 | `test_stream.py`                      |
| 会话持久化 + 重启恢复 + 版本链                    | ✅ 完整 | `test_session_db.py` 570 行            |
| 评测框架（24 样本 / 7 类别 / 跨模型对比）            | ✅ 完整 | `--compare` 已产出 DeepSeek vs MiMo 对比   |
| 多模型接入（DeepSeek / MiMo / GLM）          | ✅ 完整 | `scripts/eval_model.py`               |
| 桌面版（pywebview）                        | ✅ 可用 | 单进程 + 后台线程 + 端口从 socket 读回            |
| 前端（阶段条 / 骨架屏 / 版本切换 / 分享图）            | ✅ 可用 | 类型检查通过                                |

**半成品 / 已知缺口**

| 缺口                  | 现状                                                              | 影响                                     |
| ------------------- | --------------------------------------------------------------- | -------------------------------------- |
| **前端模型切换器**         | `deepseek-chat` 是**装饰性按钮**（`<span>` + 按钮样式，`tabIndex=-1`），点了没反应 | 后端已支持多模型（改 3 个环境变量），前端没有入口             |
| **`train`（普速列车）参数** | `MODE_PROFILE["train"]` 是死代码，实际走高铁档                             | 普速估算偏高约 2 倍（实测上海→成都返回 8.9h，普速应约 16.9h） |
| **进度百分比**           | 刻意不做（用阶段数代替）                                                    | 设计取舍，非缺陷                               |
| **多实例部署**           | 内存会话表 + 本地 SQLite                                               | 要水平扩展需换 Redis + 共享存储，接口不变              |
| **鉴权**              | 无。任何客户端可读/删任意会话                                                 | 单机单用户工具可接受，但部署到公网前必须补                  |
| **证据复用**            | 修订时全部重查工具                                                       | 刻意为之（防「用过时数据算出的新计划」），代价是慢              |

### 1.6 简明摘要（可直接引用）

> **Cairn** 是一个把自然语言任务拆解为结构化执行计划的 Agent 系统。它的技术主张是：**LLM 负责语义分解，编排层负责正确性兜底**。
>
> 架构上分为四层：编排循环（状态机 + 7 类护栏）、工具层（10 个工具经 MCP 协议暴露，本地/远程双通道）、接口层（CLI / SSE Web / 桌面三端共用同一管道）、数据层（SQLite 会话库 + JSONL 运行日志 + 本地城市/景点数据）。
>
> 三个最有辨识度的设计：**① 用 schema 条件校验而非提示词来强制事实声明**（引用了时效性数据源就必须写 `data_freshness`，否则校验失败触发修复）；**② 城市解析本地优先**（在线地理编码对中文名会静默返回错误城市，这比报错更危险）；**③ 事件流是唯一实现完整逻辑的入口**（`run()` / SSE / 桌面都是它的消费者，杜绝了三端逻辑分叉）。
>
> 工程质量显著高于同规模原型：约 11,000 行手写 Python 配 8,626 行测试（654 项全绿），注释解释「为什么」而非「是什么」，每个非显然的取舍都记录了踩坑过程。主要短板在**工程基础设施**（无 linter / formatter / 类型检查 / CI）和**文档漂移**（README 仍写「8 个工具 / 282 条景点 / 193 项测试」，实际为 10 / 6,103 / 654）。

---

## 二、代码评审问题列表

> 每条包含：**问题描述 + 位置 + 修改建议**。按严重程度排序。  
> 标注 ✅ 的条目已通过实测验证，非静态推断。

### 2.1 严重（真 Bug，会产生错误结果或崩溃）

#### 🔴 S1. `train`（普速列车）模式误用高铁参数 ✅ 实测验证

- **位置**：`mcp_server/tools/route.py:109`、`route.py:114`、`route.py:451-453`
- **描述**：`MODE_PROFILE` 里定义了 `"train": (1.22, 120.0, 0.8, 0.20)` 并注释「普速，比高铁慢且便宜」，但 `RAIL_MODES = {"hsr", "train"}` 把两者都路由到 `_estimate_rail()`，而后者只读 `HSR_BANDS`（200–310 km/h、¥0.44–0.56/km）。**`MODE_PROFILE["train"]` 从未被读取，是死代码。**
- **实测**：
  ```
  train  -> duration_hours: 8.9   distance_km: 2009.5
  hsr    -> duration_hours: 8.9   distance_km: 2009.5   ← 完全相同
  drive  -> duration_hours: 20.6  distance_km: 1911.2
  ```
  按普速参数（120 km/h）应为约 16.9h / ¥405，实际返回 8.9h / ¥854 —— **时间少一半、价格高一倍**，且 `mode_label` 显示「普速列车」，对用户是双重误导。
- **建议**：把 `train` 移出 `RAIL_MODES`，让它走 `_estimate_other()`；或为普速单独建分档表。同时补一条单测断言 `train` 与 `hsr` 的时长**必须不同**。


#### 🔴 S2. MCP Server 在 async 回调里做同步阻塞 I/O ✅ 已验证

- **位置**：`mcp_server/server.py:60`（`on_call_tool`）
- **描述**：`on_call_tool` 声明为 `async def`，但直接同步调用 `invoke()`。而 `invoke()` 内部会发起 httpx 同步请求（天气、网页抓取 1–2 秒），并在重试时 `time.sleep()`。**整个 MCP Server 的事件循环被阻塞**：无法并发处理多个工具调用，超时期间服务完全无响应。
- **对比**：`LocalToolRunner.call()`（`agent/tool_runner.py:44`）已经正确使用了 `asyncio.to_thread(invoke, ...)`，注释里也写明了原因。MCP Server 这条路径漏了。
- **建议**：改为 `result = await asyncio.to_thread(invoke, params.name, dict(params.arguments or {}))`。

#### 🔴 S3. 打包配置漏了 `web` 和 `desktop` 包 ✅ 已验证

- **位置**：`pyproject.toml:49` vs `pyproject.toml:25-26`
- **描述**：`[project.scripts]` 注册了 `task-planner-web = "web.serve:main"` 和 `task-planner-desktop = "desktop.app:main"`，但 `[tool.hatch.build.targets.wheel]` 只打包了 `packages = ["agent", "mcp_server", "common"]`。**安装 wheel 后执行 `task-planner-web` 会 `ModuleNotFoundError: No module named 'web'`。**
- **影响范围**：`uv sync` 本地开发时因为源码目录就在路径上，问题被掩盖；只有真正打包分发才暴露。
- **建议**：`packages = ["agent", "mcp_server", "common", "web", "desktop"]`。顺带确认 `web/static`（前端构建产物）是否需要作为 package data 一起打包。

#### 🔴 S4. 前端提交无 in-flight 守卫，可重复创建会话

- **位置**：`frontend/src/hooks/usePlanningSession.ts:460-479` + `App.tsx:365`
- **描述**：`start()` 先 `dispatch({type:'leaving'})`（此时 `phase` 未变，`busy` 仍为 `false`），再 `await createSession(...)`。在网络往返的窗口期内，输入框的提交按钮仍可点击，**第二次点击会再建一个会话**，服务端残留孤儿会话（既消耗 LLM 额度，又会在「最近」里出现重复记录）。
- **建议**：加 `submittingRef`（`useRef` 同步置位，避免 setState 异步导致的判断延迟），或在 `dispatch` 的同时立即置 `phase: 'running'`。

#### 🔴 S5. 前端历史会话切换无竞态防护

- **位置**：`frontend/src/hooks/usePlanningSession.ts:515-560`（`restore` / `start`）
- **描述**：快速连点两条历史记录，两个 `getSession` 请求并发；慢的那个后返回会**覆盖掉后选中的那条**，界面显示的内容与侧栏高亮项不一致。没有请求序号，也没有 `AbortController`。
- **建议**：维护递增 `requestId`，响应回来时比对，过期则丢弃；或对每个请求挂 `AbortController`，新请求前先 `abort()` 旧的。

#### 🔴 S6. SSE 重连存在重入与盲重试

- **位置**：`frontend/src/client.ts:194-208`
- **描述**：两处问题：
  1. **重入**：`onerror` 内先 `source = null` 再排定时器；若 `onerror` 连续触发两次，第一次的 `retryTimer` 引用被覆盖，定时器泄漏（且会创建两条并发连接）。
  2. **盲重试**：对 4xx（会话已被删除 / 不存在）也会重试 5 次，用户要等约 15 秒才看到「连接中断」，而实际原因是「这个会话没了」。
- **建议**：`onerror` 开头先判 `if (retryTimer !== null) return;`；区分永久失败（会话 404）与瞬时失败，前者立即终止并给出明确提示。

#### 🔴 S7. 高德类工具未校验覆盖范围，境外会**静默返回错误结果** ✅ 已实测并修复

> 这条是用户实际报上来的（截图：伦敦行程里两条查证显示「没有数据」）。

- **位置**：`mcp_server/tools/attraction_live.py`、`mcp_server/tools/transit.py`
- **描述**：两个工具只做了「城市能否解析」这一道检查，而**伦敦 / 巴黎都能解析**
  （它们在 1.4 万城市索引里），于是照样去查高德。高德对不认识的 `region`
  **不报错，而是静默降级成全国关键词模糊搜索**：

  | 查询 | 高德实际返回 |
  |---|---|
  | 大英博物馆 @ 伦敦 | 遂宁市大英县的「**大英汉陶博物馆**」（「大英」命中了） |
  | 伦敦塔桥 @ 伦敦 | 北京世界公园里的「**世界公园-伦敦塔桥**」微缩景观 |
  | 埃菲尔铁塔 @ 巴黎 | 无结果 |
  | 上海 → 伦敦（换乘） | `INSUFFICIENT_ABROAD_PRIVILEGES`（境外服务需单独申请权限） |

  三个后果，按严重程度：

  1. **可能返回错误数据。** `cities_conflict()` 在拿不到 `cityname` 时**刻意放行**
     （注释写着「拿不到城市就不据此拒绝，交给名称相似度」）。
     也就是说，若高德返回一条 `cityname` 为空的国内 POI 且名称相似度 ≥ 0.60，
     工具会把它**当成境外景点返回**——这正是项目最在意的那条线。
     我的修复（调用前判覆盖范围）把这条路径彻底堵死。
  2. **白烧一次配额**（个人开发者 10,000 次/月、2 QPS）。
  3. **失败原因误导。** 换乘接口把 `INSUFFICIENT_ABROAD_PRIVILEGES` 包成
     「可能两地之间没有公共交通方案」—— 上海→伦敦当然没有铁路方案，
     但原因不是「没有」，是「查不了」。用户会因此以为真的去不了。

- **✅ 已修**：新增 `route.amap_covers(city)`（按 `lookup_city().country` 判定），
  两个工具都在**发起请求之前**判掉，并给出可执行的替代方案
  （`query_attractions_db` / `estimate_route`），且明确写出
  「**这是覆盖范围问题，不是景点不存在 / 不是两地之间没有交通**」。

- **覆盖范围是实测出来的，不是猜的**（同一把 key）：

  | 查询 | 结果 |
  |---|---|
  | 东方明珠 @ 上海 | ✅ 正确 |
  | 维多利亚港 @ 香港 | ✅ 正确（86 条） |
  | 大三巴牌坊 @ 澳门 | ✅ 正确（96 条） |
  | 台北101 @ 台北 | ❌ 空 |
  | 大英博物馆 @ 伦敦 | ❌ 返回遂宁结果 |

  **台湾特别容易想当然**：`中国台湾` 不在覆盖范围内，已写进常量注释与测试。

- **回归测试**：`test_tool_rejects_overseas_before_calling`（参数化 4 城，
  用会抛异常的假 HTTP client 断言**根本没发起请求**）、
  `test_amap_covers_domestic_and_hk_mo` / `test_amap_does_not_cover_overseas`、
  `test_rejects_overseas_before_calling`（换乘）。

- **同类问题的第三处**：`query_attractions_db` 把「城市未收录」与
  「有城市但筛选条件筛空」混成同一句话。已拆开 —— 前者该换数据源，后者该放宽条件。

### 2.2 中等（正确性风险 / 性能 / 资源）

#### 🟠 M14. 天气工具的网络异常被归成笼统的 `UNEXPECTED`（**原报告误标为「严重 S6」，此处更正**）

> **更正说明**：本节初版写作「严重 S6」，并声称「已经写好的降级路径根本不会被执行」。  
> 动手修复时复核发现**两处失实**，故降级为「中等」并改写：  
> ① `weather.py` 里**没有**「降级到本地城市表」的数据兜底路径 —— `HTTPStatusError` 分支产出的是  
> `OUT_OF_RANGE` / `UPSTREAM_ERROR` 加可执行提示语，是**错误消息质量**的路径，不是数据降级；  
> ② 异常冒泡到 `invoke()` 后**仍会重试**（`ERR_UNEXPECTED` 在 `RETRYABLE` 集合里），  
> 且 `TimeoutException` 会被 `_is_timeout()` 按类名识别、映射成 `ERR_UPSTREAM_TIMEOUT`。  
> 所以问题不是「不重试」或「降级失效」，而是**错误码语义不准、丢失出错阶段**。

- **位置**：`mcp_server/tools/weather.py:144`（`resolve_city` 里的 `_fetch_json`）、`weather.py:264-279`（预报抓取）
- **描述**：`resolve_city()` 里的地理编码调用**没有 try**；预报抓取只 `except httpx.HTTPStatusError`。而 `httpx.TimeoutException` / `httpx.ConnectError` **都不是 `HTTPStatusError` 的子类**（三者同属 `httpx.HTTPError` 的兄弟分支），因此会一路冒泡到 `base.invoke()` 的兜底分支：
  - `TimeoutException` → 被 `_is_timeout()` 按类名捕获 → `ERR_UPSTREAM_TIMEOUT`（可重试，语义正确，但无阶段信息）
  - `ConnectError` / `ReadError` 等 → **`ERR_UNEXPECTED`**（可重试，但错误码说不清是「网络慢」还是「上游挂了」）
- **实际影响**：功能可用、会重试。真实代价是**排障时错误码给不出方向**，且「出错在城市解析阶段还是预报阶段」这个信息完全丢失。
- **已验证的修复**（新增 `_upstream_failure()` 辅助函数 + 两处 `except httpx.HTTPError`）：
  | 场景                                                                                                                                                                                          | 修前                        | 修后                                  |
  | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- | ----------------------------------- |
  | 地理编码超时                                                                                                                                                                                      | `UPSTREAM_TIMEOUT`（无阶段信息） | `UPSTREAM_TIMEOUT` + 「城市解析（在线地理编码）」 |
  | 地理编码连接失败                                                                                                                                                                                    | `UNEXPECTED`              | `UPSTREAM_ERROR` + 阶段信息             |
  | 预报接口超时                                                                                                                                                                                      | `UPSTREAM_TIMEOUT`（无阶段信息） | `UPSTREAM_TIMEOUT` + 「天气预报接口」       |
  | 真·查无此城                                                                                                                                                                                      | `NOT_FOUND`               | `NOT_FOUND`（未变，语义仍正确）               |
  | 日期越界（400）                                                                                                                                                                                   | `OUT_OF_RANGE`            | `OUT_OF_RANGE`（未变）                  |
  | 两个实现要点：`except httpx.HTTPStatusError` 必须排在 `except httpx.HTTPError` **之前**（后者是前者的父类，顺序反了会把状态码错误一起吞掉）；`resolve_city` 的网络异常**继续向上抛**而不是吞成「查无此城」—— 否则一次网络抖动会让模型以为城市不存在而去换城市。已补 3 条回归测试钉住这五条路径。 |                           |                                     |


#### 🟠 M1. SQLite 连接未显式关闭 ✅ 已验证

- **位置**：`mcp_server/tools/attractions.py:98`、`attractions.py:219`、`mcp_server/data/seed.py:736`
- **描述**：`with connect() as conn:` —— **`sqlite3.Connection` 的上下文管理器只提交/回滚事务，不关闭连接**。连接依赖 CPython 引用计数回收；一旦被 traceback 或异常栈持有，Windows 上重播库时的 `DROP TABLE`（`seed.py:724`）会报 `database is locked`。
- **对照**：`web/session_db.py:149-161` 的 `_connect()` 用了 `try/finally: conn.close()`，是正确写法。**同一个项目里两种写法并存**，说明这是遗漏而非有意。
- **建议**：统一改为 `contextlib.closing(connect())`，或给 `connect()` 本身加上 `@contextmanager` 包装。

#### 🟠 M2. 会话落盘在事件循环里做同步 I/O

- **位置**：`web/session_db.py:192`（`save`）+ `web/session.py:147`（`_persist`，由 `set_status` 同步调用）
- **描述**：`Session.set_status()` 是 async 方法，其内部 `_persist()` 同步调用 `SessionDB.save()` → 完整 JSON 序列化 + INSERT + `_prune` + commit。**整个磁盘写发生在事件循环线程上**。单会话时约几毫秒可忽略；多会话并发时会累积成可感知的卡顿，且 SSE 心跳（15 秒一次）可能被推迟。
- **建议**：`_persist` 改为 `asyncio.to_thread`（注意 `dropped` 标志的读取要保留在主线程判断）。

#### 🟠 M3. `_prune` 每次写入都执行全表子查询

- **位置**：`web/session_db.py:225-233`
- **描述**：每次 `save()` 都跑 `DELETE ... WHERE id IN (SELECT id FROM sessions ORDER BY created_at DESC LIMIT -1 OFFSET 500)`。这是每次写入都做一次排序 + 子查询。500 行时影响有限，但**99.8% 的调用是不必要的**（表根本没满）。
- **建议**：先 `SELECT COUNT(*)`（有 `idx_sessions_created_at` 索引，很快）或维护一个内存计数器，仅在超过 `MAX_ROWS` 时执行 prune。另外建议启用 WAL：`conn.execute("PRAGMA journal_mode=WAL")`，显著改善读写并发。

#### 🟠 M4. 限流器内存无上限

- **位置**：`web/app.py:157-179`（`RateLimiter`）
- **描述**：`self._hits: dict[str, list[float]]` 以客户端 IP 为 key，**从不删除 key**。列表本身在访问时会被裁剪，但 key 永久保留。来自大量不同 IP 的请求（或反向代理配置错误导致 IP 爆炸）会让字典无限增长。
- **建议**：在 `_janitor_loop` 里定期清理空列表的 key；或换用 `cachetools.TTLCache`。

#### 🟠 M5. LLM 客户端与 ToolRunner 从不关闭

- **位置**：`web/app.py:624-645`（`_drive_session`）、`app.py:648-684`（`_drive_revision`）
- **描述**：每次运行都新建 `DeepSeekClient`（内部持有 `httpx.Client`）和 `LocalToolRunner`，但**从不调用 `aclose()`**，也不使用 `async with`。OpenAI SDK 的 httpx 连接池依赖 GC 释放，大量会话后会累积 socket / 文件描述符。
- **建议**：用 `try/finally` 或在 `Orchestrator` 上加 `aclose()`，在 `_drive_*` 的 finally 中调用。

#### 🟡 M6. 前端事件数组 O(n²) 复制 —— **实测可忽略，不改**

- **位置**：`frontend/src/hooks/usePlanningSession.ts:273`
- **描述**：每个事件都 `[...state.events, event]` 全量复制数组。一次运行 10–30 个事件尚可，但**长会话 + 多次修订**会让事件数持续累加（事件是追加到同一 `Session.events` 的，修订不清空），复制成本随之平方增长。
- **建议**：改用 `useReducer` 内的原地 push + 版本号触发，或对已终态的历史事件做裁剪（保留最近 N 条 + 聚合摘要）。

#### 🟠 M7. `buildProbes` 重复全量计算，无 memo

- **位置**：`frontend/src/components/EvidenceList.tsx:182`（`buildProbes`）、`EvidenceList.tsx:233`（`StageLine` 内再算一遍）、`App.tsx:282`（`events.filter`）
- **描述**：同一份 `events` 被 `EvidenceList` 和 `StageLine` 各遍历一遍；`App` 每收到一个事件就全树重渲染。`PlanView` 也未 `memo`，修订流式期间每个事件都重渲染整份计划 DOM。
- **建议**：`const probes = useMemo(() => buildProbes(events), [events])` 后向下传；`React.memo` 包住 `PlanView` / `EvidenceList`。

#### 🟠 M8. 查看历史失败版本会污染当前会话状态

- **位置**：`frontend/src/hooks/usePlanningSession.ts:239-251`
- **描述**：`showVersion` 直接令 `phase = result.ok ? 'done' : 'failed'`。用户只是想**看看**第 2 版（一个失败的修订），整个会话却被标记为失败态——阶段条、颜色、提示全部改变。
- **建议**：区分「会话状态」与「正在查看的版本状态」，版本切换只影响展示区，不改 `phase`。

#### 🟠 M9. 菜单键盘不可达

- **位置**：`frontend/src/components/Composer.tsx:100-102`、`ShareButton.tsx:189-190`
- **描述**：`VersionMenu` / `ShareButton` 使用了 `role="menu"`，但打开后**焦点仍留在触发按钮上**，方向键处理挂在菜单容器上——键盘用户无法进入菜单导航。`VersionMenu` 打开时也不聚焦首项。
- **建议**：打开时 `focus()` 首项，`↑/↓` 移动焦点，`Esc` 关闭并把焦点还给触发按钮（符合 WAI-ARIA Menu 模式）。

#### 🟠 M10. `currency` 降级提示语误导

- **位置**：`mcp_server/tools/currency.py:117-136`
- **描述**：在线汇率获取**成功**、但目标币种不在返回的 `rates` 里时，会静默回落到离线表，而 `note` 却写「在线汇率不可用」（`:136`）。这与实际原因不符，排查时会往网络方向找。
- **建议**：区分两种情况，分别写「在线汇率服务不可用」与「在线汇率不含 {币种}，已用离线表」。

#### 🟠 M11. `load_runs` 全文件读入内存

- **位置**：`agent/run_log.py:273`
- **描述**：`file.read_text(...).splitlines()` 把整个日志读进内存再切分。`MAX_LOG_BYTES` 是 50MB，加上 `.1` 轮转文件，峰值内存可达 200MB+。
- **建议**：改为逐行流式读取（`for line in file:`），并在遇到坏行时继续。

#### 🟠 M12. 评测串行执行，无并发选项

- **位置**：`agent/eval_runner.py`（全局搜索无 `ThreadPool` / `Executor` / `--jobs`）
- **描述**：24 个样本串行跑。MiMo 实测平均 100 秒/条 → **全量约 40 分钟**，而这期间无法做别的事。
- **建议**：加 `--jobs N`（默认 1，保持可复现），用 `ThreadPoolExecutor` 并发。注意上游速率限制：建议默认 `jobs=2~4` 并加指数退避；同时把每条样本的 `duration_ms` 单独记录，避免并发下墙钟时间失去意义。

#### 🟠 M13. 配置非法值静默回落，无任何告警

- **位置**：`agent/config.py:35-48`（`_env_int`）、`config.py:51-61`（`_env_float`）
- **描述**：`MAX_TURNS=-1` 会静默回落到 8。docstring 明确说这是为了修「静默接受 -1」的问题，但修法是**从「静默接受」变成「静默忽略」**——用户仍然不知道自己写错了。`loop.py:397-399` 那条 `max_turns<1` 的告警因此**永远不可达**（env 路径已被 `_env_int` 拦掉）。
- **建议**：回落时 `print(..., file=sys.stderr)` 一行告警，说明「{name}={raw} 非法，已回落到 {default}」。

### 2.3 轻微（可读性 / 一致性 / 潜在问题）

| #   | 问题                                    | 位置                                                                 | 建议                                                                                                                                                                                  |
| --- | ------------------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| L1  | `Any` 用于类型注解但未导入 ✅                    | `mcp_server/tools/route.py:364`、`attractions.py:146`               | 加 `from typing import Any`。当前靠 `from __future__ import annotations` 把注解变字符串才不报错；一旦调用 `get_type_hints()` 或移除 future import 就会 `NameError`                                            |
| L2  | Haversine 缺 clamp                     | `route.py:292`、`attraction_live.py:169`                            | `asin(sqrt(a))` 中 `a` 因浮点误差可能略 >1 → `ValueError`。改 `min(1.0, sqrt(h))`                                                                                                              |
| L3  | MCP instructions 写「8 个工具」实际 10 个 ✅    | `mcp_server/server.py:71-72`                                       | 改为动态拼接 `len(TOOL_REGISTRY)`。这段文字会进 LLM 上下文                                                                                                                                          |
| L4  | 时区硬编码                                 | `mcp_server/tools/weather.py:175`                                  | 地理编码未返回时区时默认 `"Asia/Shanghai"`，境外城市会错算日期边界                                                                                                                                          |
| L5  | `getContext('2d')!` 非空断言              | `frontend/src/lib/shareImage.ts:54`                                | 极少数环境返回 `null` 会直接崩。判空后抛明确错误                                                                                                                                                        |
| L6  | 剪贴板写入无错误处理                            | `frontend/src/App.tsx:477`                                         | `void navigator.clipboard.writeText(...)` 无 `.catch`；非 https 或 `navigator.clipboard` 为 `undefined` 时静默失败，用户以为复制成功了                                                                  |
| L7  | 「点外部 / Esc 关闭」逻辑三处重复                  | `Composer.tsx:69-83`、`ShareButton.tsx:108-125`、`ConfirmDialog.tsx` | 抽 `useDismissable(ref, onClose)`                                                                                                                                                    |
| L8  | `_ensure_schema` 存在竞态                 | `web/session_db.py:163-172`                                        | `self._ready` 检查与赋值非原子；FastAPI 线程池下两个线程可能同时跑 DDL，`ALTER TABLE` 撞车时被宽 `except` 吞掉 → 那次写入静默失败。建议加 `threading.Lock`                                                                    |
| L9  | `append_record` 非并发安全 + 轮转 TOCTOU     | `agent/run_log.py:195-212`                                         | 多线程/多进程下可能写出交错行；`_rotate_if_needed` 与 `open` 之间是竞态窗口。建议加文件锁（`msvcrt.locking` / `fcntl.flock`）                                                                                       |
| L10 | `custom DB` 字段假设                      | `mcp_server/tools/attractions.py:120-140, 249, 260`                | `_row_to_dict` 只为 `source` 列做防御；`price` / `duration_hours` 求和时遇 NULL 会 `TypeError`。默认库有 NOT NULL 约束，仅自定义 `ATTRACTIONS_DB` 受影响                                                       |
| L11 | 时间显示不自动刷新                             | `frontend/src/lib/formatTime.ts:9`、`Composer.tsx:38`               | 「刚刚 / 今天」只在渲染时计算，页面长时间停留会显示过期的时间。可加一个 60 秒的定时重渲染                                                                                                                                    |
| L12 | 魔法数跨文件耦合                              | `frontend/src/App.tsx:21-23`（`HERO_EXIT_MS = 420`）                 | 必须与 `styles.css` 的 `.exiting` 过渡时长一致，注释说明了但无编译期保障。建议用 CSS 自定义属性 + `getComputedStyle` 读取                                                                                             |
| L13 | `ConfirmDialog` 的 keydown effect 频繁重绑 | `frontend/src/components/ConfirmDialog.tsx:102`                    | 依赖 `onCancel`，而 `App.tsx:514` 传内联箭头函数 → 弹窗打开期间每次渲染都重绑监听。用 `useCallback` 或 `useRef` 存回调                                                                                              |
| L14 | import 期触发数据库 I/O                     | `mcp_server/tools/attractions.py:188`                              | 导入时调 `_coverage_note()` → `connect()` → 可能触发 `seed()`。而每次 `connect()` 都会跑 `_needs_reseed` 的 COUNT 查询（`seed.py:683-697`），`query_attractions_db` 又是每请求都 connect。建议把「覆盖率说明」改成惰性求值 + 缓存 |


### 2.4 安全与风险

| #        | 问题                                   | 位置                                       | 评估与建议                                                                                                                                                                                                                                                                                                                                                         |
| -------- | ------------------------------------ | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SEC1** | **SSRF 存在 DNS rebinding TOCTOU**     | `web.py:91-110`（校验）vs `web.py:128`（实际请求） | **这是本报告中最值得重视的安全问题。** `host_is_blocked()` 解析 DNS 并校验 IP，但随后 httpx **会自己再解析一次**。恶意 DNS 可以对第一次查询返回公网 IP（通过校验）、对第二次返回 `127.0.0.1`（实际连内网）。项目的威胁模型明确写着「防 SSRF」，因此这是实质性缺口。**建议**：解析后把 IP 固定下来，直接连该 IP 并设置 `Host` 头（httpx 支持 `transport` 自定义）；或改用 `httpx` 的 `resolver` 钩子。另外 `host_is_blocked` 在 `socket.gaierror` 时返回 `None`（放行），与 rebinding 叠加会放大风险——建议改为「解析失败即拒绝」 |
| **SEC2** | 无鉴权，可读/删任意会话                         | `web/app.py:342-355`（读）、`:471-478`（删）    | session_id 是 `uuid4().hex[:16]`（64 bit 熵），**不可枚举**，因此实际风险低。但这是「靠不可猜测性」而非「靠鉴权」。若部署到公网或多人环境，必须补 token 或至少限制为 `127.0.0.1` 绑定                                                                                                                                                                                                                                     |
| **SEC3** | `ALLOW_PRIVATE_URLS=1` 是一键关闭 SSRF 防护 | `web.py:80-81`                           | 注释已明确「仅用于本地开发与测试」。建议加一道保险：若检测到该变量为真**且**绑定地址非 loopback，则在启动时打印显著警告                                                                                                                                                                                                                                                                                            |
| **SEC4** | 无请求体大小限制                             | `web/app.py:69-87`                       | `task` 限 500 字符、`feedback` 限 2000 字符（Pydantic `max_length`），已覆盖。但 `save_itinerary` 的 `content` 无上限，模型可写入超大文件。建议加 `max_length` 或字节上限                                                                                                                                                                                                                           |
| **SEC5** | 无 CSRF 防护                            | `web/app.py:262-267`                     | CORS 只放行 `localhost:5173`，且无 Cookie 鉴权（无会话凭证），CSRF 影响有限。但 `POST /api/sessions` 会消耗 LLM 额度——恶意页面可通过表单 POST 触发（简单请求不受 CORS 预检限制）。建议校验 `Origin` 头或要求自定义请求头                                                                                                                                                                                                       |

### 2.5 工程质量基础设施（缺口）

| 缺口                       | 现状                                        | 影响                                                                                          |
| ------------------------ | ----------------------------------------- | ------------------------------------------------------------------------------------------- |
| **无 linter / formatter** | 无 `ruff` / `black` / `flake8` 配置          | 代码风格靠人自觉。以本项目的注释密度，风格漂移会很快显现                                                                |
| **无静态类型检查**              | 无 `mypy` / `pyright`                      | 类型注解写得很好但**没有任何工具在验证它们**。L1（`Any` 未导入）就是这类问题                                                |
| **无前端 lint**             | 无 `eslint` / `prettier`                   | 同上                                                                                          |
| **无 CI 配置**              | 无 `.github/workflows`                     | 「654 项测试全绿」只能靠人工在本地跑。PR 场景下无法自动拦截回归                                                         |
| **无覆盖率统计**               | 无 `pytest-cov`                            | 测试量很大，但**不知道哪里没被测到**                                                                        |
| **文档漂移**                 | README 写「8 个工具 / 42 城市 282 条景点 / 193 项测试」 | 实测为 **10 个工具 / 305 城市 6,103 条景点 / 654 项测试**。数据规模已增长 20 倍，README 未同步。**这类漂移会误导使用者对数据覆盖度的判断** |

---

## 三、迭代优化路线

### 3.1 短期（低改动成本，建议 1–2 天内落地）

#### 功能迭代

| 项  | 动作                     | 涉及文件                        |
| -- | ---------------------- | --------------------------- |
| F1 | 修 `train` 模式参数（S1）     | `mcp_server/tools/route.py` |
| F2 | 修打包配置（S3）              | `pyproject.toml`            |
| F3 | 前端提交加 in-flight 守卫（S4） | `usePlanningSession.ts`     |
| F4 | 前端历史切换加请求序号（S5）        | `usePlanningSession.ts`     |
| F5 | SSE 重连去重 + 区分永久失败（S7）  | `frontend/src/client.ts`    |
| F6 | 版本切换不再污染 `phase`（M8）   | `usePlanningSession.ts`     |

#### 实验迭代

| 项  | 动作                                                               |
| -- | ---------------------------------------------------------------- |
| E1 | 给 `route.py` 补一条断言「`train` 与 `hsr` 结果必须不同」的单测                    |
| E2 | 给 `weather.py` 补「网络异常时降级到本地城市表」的单测（用 `respx` 或 monkeypatch 模拟断网） |
| E3 | 跑一次同口径的 DeepSeek 基线（24 样本），消除 MiMo 对比中的判定口径差异                    |

#### 工程质量迭代

| 项  | 动作                                                     |
| -- | ------------------------------------------------------ |
| Q1 | 接入 `ruff`（lint + format），配置写进 `pyproject.toml`，先只报错不阻断 |
| Q2 | 统一 SQLite 连接写法为 `contextlib.closing`（M1）               |
| Q3 | 同步 README 的过时数字（工具数 / 城市数 / 景点数 / 测试数）                 |
| Q4 | 修 `Any` 未导入（L1）、Haversine clamp（L2）、工具数文案（L3）          |

### 3.2 中期（新增能力 / 指标优化，建议 2–6 周）

#### 功能迭代

| 项   | 动作                                       | 价值                                                                                                          |
| --- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| F7  | **前端模型切换器**（把 `deepseek-chat` 假按钮做成真选择器） | 后端已支持多模型，缺的只是入口。需要：`Settings` 支持多模型配置、会话创建时带模型选择、`chat()` 接受 `model` 参数                                     |
| F8  | **鉴权层**（SEC2）                            | 部署到公网/多人环境的前置条件                                                                                             |
| F9  | **SSRF 加固：IP 固定**（SEC1）                  | 消除 DNS rebinding                                                                                            |
| F10 | 请求体与落盘内容大小限制（SEC4）                       | 防资源耗尽                                                                                                       |
| F11 | **工具结果摘要的 token 预算控制**                   | 现在 `ToolResult.data` 是 `Any`，无统一截断。`fetch_webpage` 有 `max_chars`，但景点库返回可能很大。建议在 `invoke()` 层加统一的 payload 上限 |

#### 实验迭代

| 项  | 动作                                                | 价值                                    |
| -- | ------------------------------------------------- | ------------------------------------- |
| E4 | **评测并发化**（M12，`--jobs N`）                         | 40 分钟 → 约 10 分钟，大幅提升迭代速度              |
| E5 | **扩充评测集**：从 24 条到 60+ 条，重点补「注入抵抗」（现在只有 3 条，统计意义弱） | 3 条样本的通过率（0%/33%/67%/100%）方差极大，无法支撑结论 |
| E6 | **建立回归基线**：每次改提示词后自动跑全量并对比                        | 现在靠人工判断「改完有没有变差」                      |
| E7 | **多轮次评测**（`--repeat 5`）区分「能力」与「运气」                | 单次结果不可信，`attempt` 字段已预留               |
| E8 | 记录并对比 `reasoning_tokens`                          | MiMo 接入时发现的问题，用量分解有助于成本分析             |

#### 工程质量迭代

| 项   | 动作                                            | 价值                |
| --- | --------------------------------------------- | ----------------- |
| Q5  | **接入 `mypy`（strict 逐步推进）**                    | 类型注解已经写得很好，只差工具验证 |
| Q6  | **接入 `pytest-cov`，设覆盖率下限**                    | 找出测试盲区            |
| Q7  | **接入前端 `eslint` + `prettier`**                | 统一风格              |
| Q8  | **落盘异步化**（M2）+ `_prune` 条件化（M3）+ 启用 WAL       | 提升并发下的响应性         |
| Q9  | **前端渲染优化**（M6/M7）：`useMemo` + `React.memo`    | 长会话下的事件流渲染        |
| Q10 | **配置非法值告警**（M13）                              | 减少「配错了但没报错」的排查成本  |
| Q11 | **资源释放**（M5）：LLM 客户端与 ToolRunner 的 `aclose()` | 防 fd 泄漏           |
| Q12 | 限流器内存回收（M4）                                   | 防内存增长             |

### 3.3 长期（架构层重构，建议 3 个月以上）

#### 功能迭代

| 项   | 动作                                                 | 说明                                                                                               |
| --- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| F12 | **多实例部署**：`SessionStore` 换 Redis，`SessionDB` 换共享存储 | 接口已经预留（`web/session.py:300` 注释明确写了）。需要处理：事件流跨实例分发（Redis Pub/Sub）、`mark_interrupted` 的分布式语义       |
| F13 | **计划执行层**（从「规划」到「执行」）                              | 当前明确「只做规划不做执行」。若要做，`Step.tool` + `tool_args` 已经是可以直接执行的契约                                        |
| F14 | **证据缓存与失效策略**                                      | 现在修订时全部重查工具（刻意为之）。若要在「不引入过期数据」的前提下提速，需要给每条证据打上「时效标签 + 有效期」，按数据类型区分（天气 6 小时 / 票价 7 天 / 开放时间 30 天） |

#### 实验迭代

| 项   | 动作                                                    | 说明                        |
| --- | ----------------------------------------------------- | ------------------------- |
| E9  | **评测集分层**：冒烟集（5 条，2 分钟）/ 标准集（24 条）/ 全量集（60+ 条）        | 现在改一行提示词就要等 40 分钟         |
| E10 | **自动化回归看板**：每次运行落库 + 趋势图                              | `runs.jsonl` 数据已经在积累，缺可视化 |
| E11 | **人工评估维度**：现在的评测全是规则断言（关键词 / 工具使用 / 步数），缺少「计划质量」的主观维度 | 可引入 LLM-as-judge 或人工打分表   |

#### 工程质量迭代

| 项   | 动作                                                                                                                                      | 说明                                                                                 |
| --- | --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Q13 | **CI/CD 流水线**：lint + typecheck + test + build 全自动                                                                                       | 前置依赖是 Q1/Q5/Q6                                                                     |
| Q14 | **`Plan` schema 的版本化**                                                                                                                  | 现在「往 Plan 加字段会让评测样本失效」是靠人记住的约定。建议引入 `schema_version` + 迁移测试                        |
| Q15 | **模块解耦**：`mcp_server/tools/` 里的工具与 `agent/` 目前通过 `TOOL_REGISTRY` 强耦合（`loop.py:97` 直接 `from mcp_server.tools.base import TOOL_REGISTRY`） | 编排层依赖了工具层的实现细节（拿 `summarize`）。若要真正支持「远端 MCP 工具」，这条依赖需要抽象成接口                        |
| Q16 | **可观测性**：接入结构化日志 + trace id                                                                                                             | 现在靠 `--verbose` 的 print 与 JSONL。多会话并发时难以定位问题                                       |
| Q17 | **配置层重构**：`DEEPSEEK_*` 变量名与「支持任意 OpenAI 兼容服务」的语义不符                                                                                      | 已在 `.env.example` 注释说明，但长期应改为 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`，并保留旧名兼容 |

## 四、行动 TODO 清单

> 按「优先级 × 改动成本」排序，可直接逐项执行。

### P0 · 立即修（真 bug，成本低）—— ✅ 7/7 已完成

- [x] **T01** 修 `train` 模式误用高铁参数（S1）—— 拆出 `HSR_MODES`，`train` 改走 `MODE_PROFILE` 线性模型。**实测 8.9h/¥854 → 17.7h/¥405**
- [x] **T02** 修打包配置漏包（S3）—— `pyproject.toml` 补 `web`、`desktop`。**实测 wheel 解包后四个入口模块均可导入**
- [x] **T03** MCP Server 异步化（S2）—— `await asyncio.to_thread(invoke, ...)`。**实测 3×1s 调用并发总耗时 1.00s（修前 3s）**
- [x] **T04** 天气工具网络异常兜底（M14）—— `_upstream_failure()` + 两处 `except httpx.HTTPError`
- [x] **T05** 统一 SQLite 连接关闭（M1）—— `connect()` 改为 `@contextmanager`
- [x] **T06** 修 `Any` 未导入（L1）
- [x] **T07** Haversine 加 clamp（L2）

### P1 · 本周内（正确性 + 用户体验）—— ✅ 7/7 已完成

- [x] **T08** 前端提交加 in-flight 守卫（S4）—— **实测连点 2 次 → 1 次 POST；临时移除守卫后复现 2 次，证明测试有区分度**
- [x] **T09** 前端历史切换加请求序号（S5）—— `requestSeqRef` 递增仲裁
- [x] **T10** SSE 重连去重 + 区分永久失败（S6）
- [x] **T11** 版本切换不再污染会话 `phase`（M8）—— 失败态的提示条上挂的是「重新开始」按钮，误触会丢计划
- [x] **T12** 菜单键盘可达性（M9）—— **实测通过**
- [x] **T13** 剪贴板写入错误处理（L6）—— **实测：屏蔽 clipboard 后给出明确原因**
- [x] **T14** 配置非法值打印告警（M13）—— **实测：合法值无噪音，非法值两条告警**

### P2 · 两周内（性能 + 资源）—— ✅ 6/7 已完成，1 项实测后判定不改

- [x] **T15** 会话落盘异步化（M2）—— **实测：0.41s 落盘期间事件循环 tick 13 次、最大间隔 32ms**
- [x] **T16** `_prune` 条件化（M3）—— **实测：写 25 行（`MAX_ROWS=10`）后稳定在 10 行且保留最新**
- [x] **T17** 资源释放（M5）—— **实测：close 恰好调用 1 次**
- [x] **T18** 限流器 key 回收（M4）
- [x] **T19** 前端渲染优化（M7）—— **实测渲染正常、无控制台错误**
- [x] **T20** `load_runs` 流式读取（M11）—— **实测：3 MB / 3 万行读取正确**
- [x] **T21** `_ensure_schema` 加锁（L8）—— **实测：冷启动 8 线程并发写入 8/8、16 线程 16/16**
- [x] ~~M6 事件数组 O(n²)~~ —— **实测 150 事件仅 0.76ms，判定不改**

### P6 · 用户报障（插入）—— ✅ 完成

- [x] **T40** 高德类工具未校验覆盖范围（S7）—— 新增 `amap_covers()`，
  `query_attraction_realtime` / `query_transit_options` 在**发起请求之前**判掉境外；
  `query_attractions_db` 区分「城市未收录」与「筛选筛空」。
  **实测：伦敦/巴黎/纽约/台北 全部前置拦截，港澳与国内正常放行**

### P3 · 一个月内（工程化基础设施）

- [ ] **T22** 接入 `ruff`（lint + format）
- [ ] **T23** 接入 `mypy`（从非严格模式起步）
- [ ] **T24** 接入前端 `eslint` + `prettier`
- [ ] **T25** 接入 `pytest-cov` 并设覆盖率下限
- [ ] **T26** 建 CI 流水线（lint → typecheck → test → build）
- [x] **T27** 同步 README 的过时数字 —— 已完成

### P4 · 安全加固（部署到非本机前必须完成）

- [ ] **T28** SSRF 加固：DNS 解析后固定 IP，消除 rebinding TOCTOU（SEC1）
- [ ] **T29** `socket.gaierror` 时改为拒绝而非放行（SEC1 附带）
- [ ] **T30** 补鉴权层，或强制绑定 `127.0.0.1`（SEC2）
- [ ] **T31** `save_itinerary` 的 `content` 加大小上限（SEC4）
- [ ] **T32** 校验 `Origin` 头防 CSRF（SEC5）

### P5 · 能力扩展（排期自由）

- [ ] **T33** 前端模型切换器（F7）
- [ ] **T34** 评测并发化 `--jobs N`（E4 / M12）
- [ ] **T35** 扩充评测集到 60+ 条，重点补「注入抵抗」（E5）
- [ ] **T36** 评测集分层：冒烟 / 标准 / 全量（E9）
- [ ] **T37** 跑同口径 DeepSeek 基线，消除 MiMo 对比的口径差异（E3）
- [ ] **T38** 建立改提示词后的自动回归对比（E6）
- [ ] **T39** 多实例部署（Redis + 共享存储）（F12）

---

## 附录：本次评审的验证方式

以下结论**经过实际执行验证**，而非静态阅读推断：

| 结论             | 验证命令 / 方式                                | 结果                                         |
| -------------- | ---------------------------------------- | ------------------------------------------ |
| 测试全绿           | `pytest -q`                              | 654 项通过，EXIT=0                             |
| `train` 模式 bug | 直接调用 `estimate_route` 对比 4 种 mode        | `train` 与 `hsr` 输出完全相同（8.9h）               |
| 工具实际数量         | `len(TOOL_REGISTRY)`                     | 10 个（README 写 8 个）                         |
| 景点库实际规模        | SQL 聚合查询                                 | 6,103 条 / 305 城市（README 写 282 条 / 42 城市）   |
| SQLite 连接未关闭   | 检查 `with connect() as conn` 用法           | 确认三处；与 `session_db.py` 的正确写法对比             |
| MCP Server 阻塞  | 阅读 `server.py:60`                        | `async def` 内直接同步调用 `invoke()`             |
| 打包漏包           | 对比 `pyproject.toml` 的 scripts 与 packages | `web` / `desktop` 未列入                      |
| `Any` 未导入      | `grep "from typing"`                     | 两文件均无 typing 导入                            |
| 无 lint 配置      | 检查仓库根目录与 `frontend/`                     | 均无 ruff / mypy / eslint / prettier / CI 配置 |
