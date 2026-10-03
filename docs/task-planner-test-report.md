# 任务规划助手 · 评测报告

> 运行命令：`uv run pytest`
> 结果：**225 passed in 59.61s** ｜ 通过率 **100%** ｜ 更新日期 2026-09-30

---

## 一、总体结果

| 测试文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `tests/test_cities.py` | 116 | 城市解析三层策略、景点库覆盖、早期错误解析的回归用例 |
| `tests/test_tools.py` | 28 | 8 个工具的行为、错误码、沙箱、重试、幂等、Schema 合规 |
| `tests/test_loop.py` | 27 | 编排循环 + 全部护栏（TC-01 ~ TC-10） |
| `tests/test_config.py` | 18 | base_url 校验、系统提示词日期注入、密钥泄漏扫描 |
| `tests/test_stream.py` | 16 | 事件流序列、JSON 可序列化、澄清挂起与恢复 |
| `tests/test_web.py` | 16 | HTTP 路由、SSE 重放/续传、澄清跨请求、不阻塞事件循环 |
| `tests/test_mcp_bridge.py` | 4 | MCP 协议桥接（真实拉起子进程，stdio/JSON-RPC 往返） |
| **合计** | **225** | **全部通过** |

测试策略：**脚本化 LLM + 真实工具**。LLM 用 `ScriptedLLM` 按剧本返回（不消耗真实 API），
但工具是真实执行的，因此测的是「编排逻辑 + 工具 + 护栏」这一整条链路，
而不是模型本身的不确定性。

此外已用**真实 DeepSeek（deepseek-chat）**做过端到端人工验证，见第六节。

---

## 二、TC-01 ~ TC-10 逐条对照

| 编号 | 场景 | 对应用例 | 断言要点 | 结果 |
|---|---|---|---|---|
| **TC-01** | 简单单域任务 | `test_tc01_simple_single_domain_task` | JSON 可被 Pydantic 解析；`phases ≥ 2`；至少 1 步 `tool == get_weather_forecast`；依赖图无环 | ✅ |
| **TC-02** | 多约束任务 | `test_tc02_multi_constraint_task` | 调用 ≥2 个不同工具；`budget_estimate` 解析值 ≤3000；出现"亲子"相关步骤 | ✅ |
| **TC-03** | 信息严重不足 | `test_tc03_ambiguous_input_triggers_clarification`<br>`test_tc03_without_interactive_channel_falls_back_to_assumptions` | 必须触发澄清中断；问题数 ≤3；答案回灌；无交互通道时降级为「合理假设」而非崩溃 | ✅ |
| **TC-04** | 事实数据依赖 | `test_tc04_factual_question_must_call_tool` | 必须调用天气工具；工具返回的真实温度（24.5℃）出现在回灌消息中；步骤带 `data_source` | ✅ |
| **TC-05** | 本地数据库查询 | `test_attractions_filter_by_price_and_tag`<br>`test_attractions_unknown_city_lists_available`<br>`test_attractions_is_idempotent` | 按 `max_price` 与标签正确过滤；未知城市返回 NOT_FOUND 并列出可用城市；幂等 | ✅ |
| **TC-06** | CSV 预算解析 | `test_parse_budget_csv_totals_match_file`<br>`test_parse_budget_csv_detects_overspend`<br>`test_parse_budget_csv_blocks_path_traversal` | 合计等于逐行之和（¥2930）；超支预警；路径越界拒绝 | ✅ |
| **TC-07** | 工具失败降级 | `test_tc07_tool_failure_degrades_gracefully`<br>`test_weather_retries_once_then_fails` | 尝试次数 ==2（含重试）；输出含 `⚠️ 数据缺失`；仍产出完整计划；错误信封回灌而非抛异常 | ✅ |
| **TC-08** | 不可行约束 | `test_tc08_infeasible_constraints_are_flagged` | 明确指出"不可行"；给出 ≥2 个调整方案；`risks ≥ 2`；不浪费工具预算 | ✅ |
| **TC-09** | 依赖排序 | `test_tc09_dependency_graph_is_acyclic`<br>`test_tc09_cyclic_plan_is_rejected`<br>`test_tc09_dangling_dependency_is_rejected` | 跨城转场步骤依赖上一城市行程；拓扑排序可完成；循环依赖与悬空依赖都被抓出 | ✅ |
| **TC-10** | 泛化 + 安全 | `test_tc10_generalizes_beyond_travel`<br>`test_tc10_prompt_injection_is_detected_and_refused`<br>`test_tc10_user_input_is_isolated_from_system_prompt`<br>`test_injection_patterns_cover_common_cases`<br>`test_benign_input_is_not_flagged` | 非旅行任务 `phases ≥ 3`；注入特征被检出；无 JSON 触发一次修复重试；用户输入被包进 `<user_task>` 且与 system 角色物理隔离 | ✅ |

### 附加护栏断言

| 护栏 | 对应用例 | 结果 |
|---|---|---|
| 工具预算（`TOOL_BUDGET`） | `test_tool_budget_is_enforced` | ✅ 超预算后拒绝调用，模型仍能收尾出计划 |
| 幂等去重 | `test_duplicate_tool_calls_hit_cache` | ✅ 重复调用命中缓存，上游只被真实请求 1 次 |
| 输出修复重试 | `test_invalid_output_triggers_one_repair` | ✅ 无 JSON → 追加纠错指令 → 拿到合规计划 |
| 不可恢复输出 | `test_unrecoverable_output_marks_failure` | ✅ `stop_reason == invalid_output`，不假装成功 |
| 轮次上限 | `test_max_turns_stops_runaway_loop` | ✅ 2 轮后强制收尾 |
| 统一信封 | `test_invoke_always_returns_envelope`<br>`test_envelope_payload_drops_none` | ✅ 永不抛裸异常；`None` 字段被裁剪 |
| Schema 合规 | `test_openai_schemas_are_wellformed` | ✅ 8 个工具均有 description，无 Pydantic `title` 噪音 |
| MCP 桥接 | `test_mcp_server_exposes_all_tools`<br>`test_mcp_call_roundtrip`<br>`test_mcp_reports_tool_error_without_crashing`<br>`test_mcp_schemas_match_local_registry` | ✅ 协议发现 8 个工具；调用往返完整；错误码透传；MCP Schema 与本地注册表逐字节一致 |

---

## 三、测试过程中发现并修复的真实缺陷

### 缺陷 11（覆盖度）：景点库只有 5 个中国城市，省会城市基本查不到

- **现象**（用户要求）："扩充到全国的省会城市"。
- **盘点**：中国有 **34 个省级行政区**，原有景点库只覆盖了其中 5 个
  （北京、杭州、成都、西安，加一个非省会的三亚）。用户提到任意一个省会
  （如哈尔滨、拉萨、乌鲁木齐）都会得到 NOT_FOUND。
- **修复**：景点库从 **12 城市 / 88 条** 扩到 **42 城市 / 282 条**：

  | 类型 | 数量 | 城市 |
  |---|---|---|
  | 直辖市 | 4 | 北京、天津、上海、重庆 |
  | 省省会 | 23 | 石家庄、太原、沈阳、长春、哈尔滨、南京、杭州、合肥、福州、南昌、济南、郑州、武汉、长沙、广州、海口、成都、贵阳、昆明、西安、兰州、西宁、**台北** |
  | 自治区首府 | 5 | 呼和浩特、南宁、拉萨、银川、乌鲁木齐 |
  | 特别行政区 | 2 | **香港**、**澳门** |
  | 热门非省会 | 1 | 三亚 |
  | 国际城市 | 7 | 纽约、东京、巴黎、伦敦、新加坡、曼谷、首尔 |

  每个城市 **≥5 个景点**，含名称、标签、人均票价、建议时长、评分、亲子友好、
  开放时间与备注。

- **顺带处理的两个问题**：
  1. **港澳台币种**：香港用 HKD、澳门用 MOP、台北用 TWD。其中 **MOP 原先不在
     离线汇率表里**，导致澳门行程算不出人民币预算 —— 已补上（1 MOP ≈ 0.889 CNY）。
  2. **策展城市表补齐**：13 个新省会原先只能靠 GeoNames 索引兜底解析，
     已提升进策展表（`cities.py` 中国城市 25 → 47 条），保证国家字段与展示名规范。
- **数据质量细节**：`open_hours` 写清季节性限制。例如哈尔滨冰雪大世界标注
  「12月-2月 11:00-21:30」——**实测中 Agent 据此主动把该项目排除出 10 月行程**，
  这是数据质量带来的真实收益。
- **回归**：新增 6 个用例，其中
  `test_attractions_covers_all_provincial_capitals` 显式断言 34 个省级行政区
  一个不缺（并校验这个集合本身就是 34 个），
  `test_attractions_dataset_size` 断言每城 ≥5 个景点，
  `test_every_provincial_capital_has_kid_friendly_options` 断言每个省会
  都有可带小孩去的地方。

### 缺陷 10（🔴 覆盖率）：`get_weather_forecast` 查不到大量城市

- **现象**（用户反馈）："这一工具无法查到许多城市的天气"。
- **量化**：对 40 个本地表未收录的城市做实测，结果触目惊心 ——
  **只有 24 个可用（60%），8 个返回错误地点（20%），8 个完全查不到（20%）**：

  | 类型 | 城市 |
  |---|---|
  | 返回**错误地点** | `开罗`→美国伊利诺伊州、`里斯本`→美国北达科他州、`米兰`→美国、`马尼拉`→美国、`日内瓦`→机场、`开普敦`→机场、`维也纳`→郊区 |
  | **无结果** | 伊斯坦布尔、华沙、雷克雅未克、内罗毕、加德满都、科伦坡、基辅、利马 |

- **根因**：把第三方在线地理编码当成了唯一解析手段。它在中文城市名上既不全也不准，
  而且错误是**静默**的 —— 返回一个"名字里有这几个字"的地方，看起来像是对的。
- **修复**：改为**三层解析**，本地优先、网络兜底：

  | 层 | 数据源 | 规模 |
  |---|---|---|
  | 1 | 策展表 `cities.py`（人工维护，含非城市目的地） | 170 条 |
  | 2 | **新增** `city_index.tsv`（从 GeoNames 生成） | **14,359 个中文城市名 / 229 个国家** |
  | 3 | Open-Meteo 地理编码（拉 10 个候选按行政级别+人口排序，带 `confidence`） | 兜底 |

  同时新增 `build_city_index.py` 构建脚本，处理了三个非显然的坑：
  1. **繁简转换**：GeoNames 里不少城市只有繁体名（`維也納`/`米蘭`/`利馬`/`基輔`/`開普敦`），
     需要 `zhconv` 转换后才能被简体输入命中；
  2. **行政区后缀归一化**：索引里是 `达沃市`、用户输入 `达沃`；索引里是 `科威特市`、
     用户输入 `科威特城`。做法是给索引键额外注册「去后缀」变体，并对查询也去后缀。
     剥离后短于 2 字的丢弃，避免 `盐城`→`盐`、`聊城`→`聊` 这类垃圾键；
  3. **国家代码 → 中文名**：GeoNames 只给 `EG` 这样的代码，
     新增 `countries.py`（229 条映射）让模型看到「埃及」。
     其中 **HK/MO/TW 一律标注为中国的一部分**，并有专门测试守住。
- **验证**：40 个失败样本全部修复；抽样 20 个城市实跑真实天气，数据均合理
  （雷克雅未克 6.7~9.4℃、马尼拉 25.8~32.0℃、特罗姆瑟 6.8~10.0℃）。
- **回归**：`tests/test_cities.py` 扩到 **111 个用例**，其中
  `test_regression_cities_resolve_to_correct_country` 用 21 个参数化用例
  把每个曾出错的城市钉死（含 `test_cairo_is_not_illinois`、
  `test_lisbon_is_not_north_dakota` 两条坐标级断言）。

### 缺陷 7（🔴 正确性）：天气工具静默返回**错误城市**

- **现象**（用户反馈）：`get_weather_forecast(city="纽约")` 返回"未找到城市"。
- **深挖后的真相比"查不到"严重得多**。直接探测 Open-Meteo 地理编码：

  | 查询 | 返回 |
  |---|---|
  | `name=纽约` | 无结果 |
  | `name=东京` `language=zh` | **命中「江苏·东京」(32.21, 119.29)** |
  | `name=New York` `language=zh` | **命中「内布拉斯加州·约克」** |
  | `name=巴黎` `language=zh` | 正确（法国巴黎） |

  也就是说，用户问东京天气，工具会**不报错地把江苏某地的天气当成东京返回**。
  这直接违反「事实数据不得臆造」这条硬约束 —— 比报错恶劣得多，因为错误被伪装成了事实。
- **根因**：完全依赖第三方地理编码解析城市名，且只取返回列表的第 1 条，
  既不校验行政级别也不校验人口。
- **修复**：
  1. 新增 `mcp_server/data/cities.py` —— **55 个城市的本地坐标表**
     （中文名 / 英文名 / 别名 → 坐标 / 国家 / 时区），天气与交通共用；
  2. `resolve_city()` 改为**本地表优先**，命中即用确定坐标，完全不依赖地理编码；
  3. 未命中才回落 API，且**拉 10 个候选**按「行政级别（PPLC>PPLA>PPL）+ 人口」排序；
  4. 返回 `resolved.{name, country, admin1, latitude, longitude, matched_via, confidence}`，
     低置信度（行政级别低且人口 <5 万）时附 `⚠️` 警告，提示模型先向用户确认；
  5. `estimate_route` 也改为共用这张表，顺带支持了英文城市名（`New York` → 纽约）。
- **验证**：`东京` 现在解析为 `日本 (35.68, 139.65)`，`纽约` / `New York` 均正确。
- **回归**：`test_weather_tokyo_resolves_to_japan_not_jiangsu`（显式断言不是江苏坐标）、
  `test_weather_resolves_international_city_from_local_table`、
  `test_weather_flags_low_confidence_match` 等 8 个用例。

### 缺陷 8（🔴 可用性）：景点库只覆盖 5 个中国城市，国际行程无法规划

- **现象**（用户反馈）：`query_attractions_db(city="纽约")` 返回 NOT_FOUND，
  计划里只能写 "⚠️ 景点库缺失"。
- **修复**：
  1. 景点库从 **5 个城市 / 39 条** 扩展到 **12 个城市 / 88 条**
     （新增纽约、东京、巴黎、伦敦、新加坡、曼谷、首尔）；
  2. 新增 `country` 与 `currency` 两列 —— 价格是**当地货币**
     （纽约 USD、东京 JPY、巴黎 EUR、伦敦 GBP、新加坡 SGD、曼谷 THB、首尔 KRW）；
  3. 汇总字段从单一 `total_price_if_all_visited` 改为 `totals_by_currency`
     （按币种分组，避免把 USD 和 CNY 加在一起）；
  4. `NOT_FOUND` 错误信息明确要求模型「不要凭记忆编造景点与票价」。
- **额外收益**：币种字段让**工具组合**自然发生 —— 实测中 Agent 自己发现门票是
  `197 USD`，主动调用两次 `convert_currency` 换算成 `¥1,324` 再算预算。
- **回归**：`test_attractions_supports_new_york_with_usd`、
  `test_attractions_totals_grouped_by_currency`、`test_attractions_covers_twelve_cities`。

### 缺陷 9（🟠 可诊断性）：预报窗口越界只报「400」，吞掉上游给的原因

- **现象**：请求 `2026-10-15` 的天气，只得到 `预报接口返回 400`，
  用户不知道是日期问题还是接口挂了。
- **根因**：`except httpx.HTTPStatusError` 分支只拼接了状态码，丢掉了响应体里的
  `reason` 字段。
- **修复**：解析响应体 `reason`，用正则抽出 `out of allowed range from A to B`，
  返回 `BAD_ARGS` + 完整可用区间 + 处置建议：

  ```
  请求的日期超出预报范围：2026-10-15 ~ 2026-10-16。
  Open-Meteo 当前只提供 2026-06-28 ~ 2026-10-14 的预报，请改用该区间内的日期；
  更远的日期只能给出气候意义上的经验判断，并需明确标注为「非实时预报」。
  ```

  错误码选 `BAD_ARGS` 而非 `UPSTREAM_ERROR`，是因为这是确定性参数问题，
  不该被当成可重试的上游故障而浪费一次重试。
- **回归**：`test_weather_out_of_range_date_reports_allowed_window`。

### 缺陷 0（🔴 安全）：真实 API Key 被写进了 `.env.example`

- **现象**：排查 404 时读取 `.env.example`，发现里面不是占位符
  `sk-xxxx…`，而是**用户真实的 DeepSeek API Key**。
- **风险等级**：高。`.env.example` 的设计用途就是**提交到 git / 分享给他人**的模板文件
  （`.gitignore` 只忽略了 `.env`，没有也不应该忽略 `.env.example`）。
  一旦提交或分享，密钥即泄漏，可能被他人盗刷额度。
- **修复**：
  1. 把 `.env.example` 恢复为纯占位符，并在文件头加上醒目警告
     「本文件会被提交到 git，只放占位符，绝不填写真实 Key」；
  2. 在 `DEEPSEEK_BASE_URL` 上方加注释说明为什么不能填 `/anthropic`；
  3. 新增两条回归测试（`tests/test_config.py`）：
     - `test_env_example_uses_placeholder_only` —— 断言占位行必须是 `sk-xxxx` 开头；
     - `test_no_real_api_key_in_committable_files` —— 全项目扫描
       （跳过 `.venv` / `.git` / `outputs` / 缓存目录），
       除 `.env` 外任何可提交文件命中 `sk-[0-9a-fA-F]{20,}` 即失败。
- **已确认**：扫描能识别真实 Key、不误报占位符；`.env.example` 已清理，
  `.env` 保持不变（它已被 `.gitignore` 忽略）。
- **⚠️ 给用户的建议**：由于该 Key 已落在模板文件中，**建议到 DeepSeek 控制台
  轮换（重新生成）这个 Key**，以防它已随文件被复制或同步到别处。

### 缺陷 4（首次真实调用即暴露）：`DEEPSEEK_BASE_URL` 误填 Anthropic 端点 → 404

- **现象**：`uv run task-planner "..."` 首次真实调用直接抛
  `openai.NotFoundError: Error code: 404`，无任何可读提示。
- **根因**：`.env` 里写的是 `https://api.deepseek.com/anthropic`。
  DeepSeek 同时提供 OpenAI 兼容端点与 **Anthropic 兼容端点**（供 Claude Code 等工具用），
  两者在文档站上挨得很近。openai SDK 会在 base_url 后拼 `/chat/completions`，
  于是请求打到 `https://api.deepseek.com/anthropic/chat/completions` → 404。
- **修复**：
  1. `.env` 改为 `https://api.deepseek.com`；
  2. `Settings.validate()` 增加已知错误后缀黑名单（`/anthropic`、`/beta`、
     完整请求路径），并给出可复制的修正方案；
  3. `DeepSeekClient.__init__` 在**构造时**就校验，不再等到第一次请求才以 404 暴露；
  4. `Settings.load()` 统一去掉结尾斜杠，避免拼出 `//chat/completions`。
- **回归**：`tests/test_config.py` 共 7 个用例覆盖该场景。

### 缺陷 5（真实调用才暴露）：模型不知道"今天"，无法推算相对时间

- **现象**：用户说"我想**周末**去成都玩两天"，Agent 反问
  "具体是哪个周末的日期？"，把本该自己推算的信息抛回给用户。
- **根因**：系统提示词里没有任何时间锚点。LLM 自身不掌握当前日期，
  而天气工具又必须传 `YYYY-MM-DD`，于是它只能猜日期或反问。
- **修复**：
  1. `agent/config.py` 新增 `render_system_prompt()`，在渲染系统提示词时注入
     `{{CURRENT_DATE}}` 与 `{{CURRENT_WEEKDAY}}`；
  2. `system.md` 增加「当前时间」章节，明确列出"周末 / 下周三 / 下个月 / 明天"
     的推算规则；
  3. 澄清门控改为**白名单式**判断：明确写出"不需要问"的情形
     （相对时间、可假设的出发地、口味偏好），避免模型拿日期骚扰用户。
- **验证**：修复后同样输入，Agent 自行推算出 `2026-10-03(周六) ~ 10-04(周日)`，
  直接调用天气工具并产出计划，**不再提问**。
- **回归**：`test_system_prompt_has_no_leftover_placeholder`、
  `test_system_prompt_teaches_relative_date_resolution`、
  `test_system_prompt_keeps_clarification_gate`。

### 缺陷 6（真实调用才暴露）：英文思考过程泄漏进中文输出

- **现象**：计划正文开头出现一句英文独白
  "I now have enough to build the plan. Note: the flight estimate..."。
- **根因**：系统提示词只写了"语言：默认简体中文"，没有约束开场简述，
  也没禁止输出内部独白。
- **修复**：`system.md` 硬性约束改为「全程使用简体中文，包括开场简述、
  冲突说明与脚注；不要输出英文的思考过程或内部独白」，
  并追加"开场不要写过程性独白，直接给结论"。
- **验证**：修复后输出从第一个字起即为中文。

### 缺陷 1：MCP 桥接丢失结构化错误码

- **现象**：`query_attractions_db(city="火星")` 本地调用返回 `NOT_FOUND`，
  但经 MCP 往返后变成 `UPSTREAM_ERROR`。
- **根因**：`MCPToolRunner.call` 先判断 `is_error` 就直接返回通用错误，
  没有解析信封里更精确的错误码。
- **修复**：调整判定顺序 —— 优先解析 JSON 信封原样透传，解析不出来才降级为 `UPSTREAM_ERROR`。
- **回归**：`test_mcp_reports_tool_error_without_crashing` 现在断言 `NOT_FOUND`。

### 缺陷 2：测试污染项目 `outputs/` 目录

- **现象**：跑完测试后 `outputs/` 多出 `x.md`。
- **根因**：`test_invoke_always_returns_envelope` 直接调用了写文件的工具，没走沙箱夹具。
- **修复**：给该用例注入 `sandbox` 夹具，把 `OUTPUT_DIR` 指向 `tmp_path`。
- **验证**：跑完测试后 `outputs/` 只剩 `.gitkeep`。

### 缺陷 3（数据质量）：景点库亲子类样本不足

- **现象**：TC-05 期望"成都 + 亲子 + 人均 ≤100"返回 ≥3 条，实际只有 2 条。
- **根因**：种子数据里符合三条件的记录偏少。
- **修复**：补充 4 条成都亲子景点（动物园、浣花溪公园、黄龙溪古镇、海昌极地海洋公园），
  并让 `connect()` 在种子行数变化时自动重建数据库，避免数据改动后本地库陈旧。

---

## 四、未覆盖 / 已知局限

| 项 | 说明 |
|---|---|
| 真实 DeepSeek 调用 | **已人工验证**（见第六节），但未纳入自动化测试（会消耗 API 额度且结果不确定） |
| 真实网络抖动 | 测试中天气 API 被 mock；实跑时已观察到一次代理抖动并成功重试（`第 1 次失败（UNEXPECTED），准备重试…`） |
| `fetch_webpage` 的真实抓取 | 只测了 URL 校验与内容类型拒绝，未对真实站点做端到端抓取（避免测试依赖外部站点稳定性） |
| 计划质量的语义评分 | 自动化只能断言结构合规，步骤是否"合理"仍需人工抽检 |
| 出发地推断 | 用户未给出发地时，模型会用"就近/常规出发地"作为假设；这可能与实际不符，需用户复核 |
| 城市索引的时效性 | `city_index.tsv` 由 GeoNames 快照生成，新增城市需重跑 `build_city_index.py` |
| 景点库覆盖 | 已覆盖中国全部 34 个省级行政区 + 7 个国际城市。其他国际城市（如伊斯坦布尔）仍会返回 NOT_FOUND，Agent 会明确标注"⚠️ 数据缺失"而非编造（已实测验证） |
| 景点价格为演示数据 | 为量级参考，非实时票价；且未接入任何真实票务 API |
| 景点数量不均 | 成都 15 条、西安/三亚 5 条，其余多为 6–8 条。作为演示数据集够用，但不是完整旅游数据库 |
| 汇率精度 | 在线汇率失败时降级到内置离线表（近似值），已在返回中显式标注 |
| 第三方数据许可 | 城市索引源自 GeoNames（CC BY 4.0），README 已标注来源 |

---

## 五、复现方式

```bash
cd task-planner
uv sync
uv run pytest            # 225 passed
uv run pytest -v         # 查看每个用例名
```

端到端人工验证：

```bash
uv run task-planner --demo -v                      # 离线全链路（真实工具）
uv run task-planner --demo --stream                # 事件流输出
uv run task-planner --demo --transport mcp -v      # 走 MCP 协议
uv run task-planner "我想去旅行" -v                  # 真实 DeepSeek（需 Key）
uv run task-planner "我想这周末去纽约玩两天，两个人" -v  # 国际行程
uv run task-planner "帮我规划这周末去伊斯坦布尔玩三天" -v  # 景点库未覆盖时的降级
uv run task-planner "规划这周末去哈尔滨玩三天，带小孩" -v  # 新增省会 + 季节性数据

uv run task-planner-web                            # 启动 Web 服务 → :8000/docs
```

---

## 六、真实 DeepSeek 端到端验证记录

**环境**：`deepseek-chat` ｜ `base_url=https://api.deepseek.com` ｜ 2026-09-29

### 用例 A：信息不足，应触发澄清

```
输入：我想去旅行
结果：轮次=2  工具调用=2  预算=2/12  成功
  ✅ ask_user_clarification  ← 正确触发澄清门控
  ✅ query_attractions_db
```

模型提出 3 个问题（出发城市 / 具体周末日期 / 人数），问题数符合"≤3"约束。
（自动化环境无 TTY，澄清回调返回空答案，模型随即走"合理假设"分支并标注假设——
这正是设计的降级行为。）

### 用例 B：修复后重跑，日期应自行推算

```
输入：我想周末去成都玩两天，带小孩，人均预算 1000
结果：轮次=3  工具调用=3  预算=3/12  成功  依赖图校验：✅ 无环
  ✅ get_weather_forecast  2454ms   ← 真实 Open-Meteo 数据
  ✅ query_attractions_db      7ms
  ✅ estimate_route            0ms
```

关键观察：

1. **不再反问日期**——自行推算出 `2026-10-03(周六) ~ 10-04(周日)`（缺陷 5 已修复）。
2. **天气是真实数据**：10-03 小毛毛雨（降水概率 41%，18.2~25.0℃）、
   10-04 毛毛雨（66%，17.3~21.5℃），并据此建议 10-04 走室内方案。
3. **景点来自本地库**：大熊猫基地 ¥55、四川科技馆 ¥0、浣花溪公园 ¥0、
   成都动物园 ¥20、黄龙溪古镇 ¥0，合计门票 ¥75/人。
4. **主动发现预算冲突**：模型自己算出跨城机票 ¥1432/人已超 ¥1000 总预算，
   于是给出"成都本地/就近出发"的默认方案 + 两个可调整方案，符合"不硬凑"约束。
5. 产出 **3 个阶段 13 个步骤**，`depends_on` 构成无环图。
6. 输出**全中文**，无英文独白泄漏（缺陷 6 已修复）。

### 用例 C：国际行程，应解析正确城市并做币种换算

```
输入：我想这周末去纽约玩两天，两个人，帮我做个计划
结果：轮次=3  工具调用=5  预算=5/12  成功  依赖图校验：✅ 无环
  ✅ get_weather_forecast  1247ms   ← 解析为「纽约 / 美国」
  ✅ query_attractions_db     9ms   ← 返回 USD 计价
  ✅ estimate_route            0ms
  ✅ convert_currency        287ms   ← 自行发现门票是美元，主动换算
  ✅ convert_currency        276ms
```

关键观察：

1. **城市解析正确**：`resolved.country = 美国`，走 `local-city-table`，未依赖地理编码。
2. **天气是真实数据**：10-03 阴 18–24℃（降水 20%）、10-04 中雨 12.7–17.8℃（43%），
   并据此把 Day2 安排成室内为主。
3. **工具组合自发产生**：门票合计 `197 USD`，Agent 自己判断需要人民币口径，
   主动调 `convert_currency` 两次，算出 `≈ ¥1,324`。
4. **主动提示预算冲突**：自己估算上海↔纽约往返机票约 ¥10,228/人，
   并指出"两天行程的机票+住宿通常远超周末短途的直觉预算"，给出调整方案。
5. 产出 **3 个阶段 16 个步骤**，`depends_on` 无环。
6. 共调用 **4 个不同工具**（weather / attractions / route / currency），
   在 12 次预算内只用了 5 次。

### 用例 D：小众城市 + 景点库缺数据，应如实说明而非编造

```
输入：帮我规划这周末去伊斯坦布尔玩三天，两个人
结果：轮次=2  工具调用=3（失败 1）  预算=3/12  成功  依赖图校验：✅ 无环
  ✅ get_weather_forecast  1258ms   ← 解析为「伊斯坦布尔 / 土耳其」
  ❌ NOT_FOUND query_attractions_db   ← 景点库不覆盖该城市
  ✅ estimate_route            0ms
```

关键观察：

1. **天气正常**：三天均为阴天，降水概率 3%–12%，气温 14–22℃，Agent 据此判断"适合户外活动"。
2. **景点缺失被如实上报**，而不是编造。Agent 的原话：

   > ⚠️ **数据缺失**：本地景点库不覆盖伊斯坦布尔（仅覆盖三亚/东京/伦敦/北京/巴黎/成都/
   > 新加坡/曼谷/杭州/纽约/西安/首尔 12 城），因此**无法提供伊斯坦布尔的景点清单与票价**。
   > 计划中涉及具体景点的部分需你自行核实或改用官方渠道查询。

3. **工具失败不中断**：`query_attractions_db` 返回 NOT_FOUND 后，Agent 继续调用
   `estimate_route` 并正常产出计划 —— 失败降级机制生效。
4. 用 `estimate_route` 估出北京↔伊斯坦布尔单程约 ¥6086/人。

### 用例 E：新增省会城市 + 季节性数据

```
输入：帮我规划这周末去哈尔滨玩三天，带小孩，人均预算 3000
结果：轮次=2  工具调用=3  预算=3/12  成功  依赖图校验：✅ 无环
  ✅ get_weather_forecast  1278ms
  ✅ query_attractions_db     14ms
  ✅ estimate_route            0ms
```

关键观察：

1. **天气**：三天降水概率 0–4%，白天约 15℃、夜间低至 3.6℃，Agent 据此提示"昼夜温差大，务必带厚外套"。
2. **季节性数据生效**：Agent 主动指出

   > ⚠️ 哈尔滨标志性的「冰雪大世界」为**冬季限定（12月–2月开放）**，10 月初**不开放**，本行程不安排该项目。

   这是 `open_hours` 字段写成「12月-2月 11:00-21:30」带来的真实收益 ——
   数据里写清楚限制，模型就能避开"看起来很合理"的错误安排。
3. **币种识别**：Agent 明确说明"本地景点库覆盖哈尔滨，价格均为人民币（CNY），无需汇率换算"。
4. 产出 **13 个步骤**，`depends_on` 无环，预算落在人均 ¥3000 上限内。

### Token 消耗参考

| 用例 | prompt | completion | 合计 |
|---|---|---|---|
| 用例 B（成都 3 轮，3 次工具调用） | 10,851 | 3,563 | 14,414 |
| 用例 C（纽约 3 轮，5 次工具调用） | 13,897 | 4,276 | 18,173 |
| 用例 D（伊斯坦布尔 2 轮，3 次工具调用） | 7,882 | 3,422 | 11,304 |
| 用例 E（哈尔滨 2 轮，3 次工具调用） | 8,015 | 3,093 | 11,108 |

---

## 七、事件流改造记录（2026-09-30）

> 这不是缺陷修复，而是为「Web 前端」做的前置改造。记录在此是因为它验证了一个
> 重要的工程判断方法。

### 背景

要让客户在网页上使用，编排循环必须从「一次性阻塞返回」改成「可流式、可暂停恢复」。
改造前有三个硬约束：

| 障碍 | 改造前 | 为什么是问题 |
|---|---|---|
| 澄清是同步阻塞的 | `answer = self.ask_user(payload)` 卡在 `input()` | Web 里没有 stdin，必须改成「暂停 → 推给前端 → 等回答 → 恢复」 |
| 没有流式输出 | 只有最终 `PlanResult` | 一次跑 10–60 秒，用户盯着转圈会以为卡死 |
| 无会话概念 | 每次调用独立 | 澄清要跨请求保持状态 |

### 改造方式

核心决定：**让 `run()` / `run_async()` 变成 `run_stream()` 的消费者，而不是重写一份逻辑。**

```
改造前：  run() / run_async()  ← 唯一实现
改造后：  run_stream()          ← 唯一实现（异步生成器，产出 Event）
              ↑
          run_async()  ← 消费事件流，返回 PlanResult
              ↑
          run()        ← 同步薄封装（asyncio.run）
```

具体改动：

1. 新增 `agent/events.py` —— `Event` 数据类 + 8 种事件类型
2. `AskUserFn` 从 `Callable[[dict], str]` 放宽为 `Callable[[dict], str | Awaitable[str]]`，
   `_resolve_clarification()` 用 `inspect.isawaitable()` 兼容同步/异步回调
3. 澄清处理从 `_execute()` **移出**到 `run_stream()` —— 因为它需要「产出事件 + 挂起」，
   而 `_execute()` 是普通协程，产不出事件
4. 新增 `run_stream()`，逐段产出 `run_started` / `turn_started` / `tool_call` /
   `tool_result` / `clarification` / `repair` / `plan_ready`
5. CLI 新增 `--stream`，用 `on_event` 回调打印事件

### 验收结果

| 检查项 | 结果 |
|---|---|
| **既有 193 个测试** | ✅ **一行没改，全部通过** —— 这是「重构行为不变」的最强证据 |
| 新增 16 个事件流测试 | ✅ 全通过 |
| 事件 `data` JSON 可序列化 | ✅ 逐事件断言（SSE 的前提） |
| 澄清事件能产出并挂起 | ✅ 异步 `ask_user` 被正确 await |
| 同步 `ask_user` 向后兼容 | ✅ CLI 与既有测试不受影响 |
| 四种运行组合 | ✅ 普通 / 流式 / MCP / MCP+流式 全部正常 |
| 真实 DeepSeek 流式运行 | ✅ 事件按序产出，计划正常 |

### 一个可复用的判断方法

> **重构时，如果既有测试需要修改才能通过，说明改动大概率不是「行为不变」的重构，
> 而是行为变更。**

本次改造前 193 个测试全部保留未动，改完直接跑绿——这个信号比任何自我审查都可靠。
唯一需要新增的是「新能力」的测试（事件流本身），而不是「修补旧测试」。

### 遗留

- `_last_result` 暂存在实例上，因此**同一 Orchestrator 实例不能并发跑多个任务**。
  Web 层应为每个会话创建独立实例。已在类文档字符串中注明。
- Web 层（FastAPI + SSE）与前端（React）尚未实现，属于 Step 2/3。

---

## 八、Web 服务层记录（2026-09-30）

> 对应 Step 2。前端（React）属于 Step 3，尚未开始。

### 交付物

| 文件 | 职责 |
|---|---|
| `web/app.py` | 路由、SSE、限流、CORS、静态托管 |
| `web/session.py` | 会话状态、事件重放、澄清挂起/唤醒、TTL 回收 |
| `web/serve.py` | 启动入口（`uv run task-planner-web`） |
| `tests/test_web.py` | 16 个用例 |

### 路由

`GET /api/health` · `GET /api/tools` · `POST /api/sessions` ·
`GET /api/sessions/{id}` · `GET /api/sessions/{id}/events`（SSE，支持 `?cursor=N`）·
`POST /api/sessions/{id}/answers` · `DELETE /api/sessions/{id}`

### 修复的两个阻塞性隐患（🔴）

这两个都不是「功能缺失」，而是**会让 Web 服务直接不可用**的问题。

**隐患 A：同步调用阻塞事件循环**

`DeepSeekClient.chat()` 与 `LocalToolRunner.call()` 都是同步的。
在 CLI 里无所谓（单用户串行），但放进异步 Web 服务后：

- 一次 LLM 调用 5–30 秒 → 期间**所有其他请求全部卡死**
- 天气工具阻塞 1–2 秒 → 同样卡死

修复：两处都包进 `asyncio.to_thread()`。
`tests/test_web.py::test_slow_llm_does_not_block_other_requests` 用「LLM 阻塞 0.5 秒，
测量 `/api/health` 响应时间必须 < 0.2 秒」来钉住这个行为。

**隐患 B：SSE 事件丢失**

如果事件推进一次性队列，客户端晚连几秒就会丢掉开头。
改为**事件保留在会话里 + 客户端带游标重放**，断线重连传 `?cursor=N` 即可续传。

### 三个非显然的设计点

1. **为什么「先 POST 建会话，再 GET 订阅」？**
   `EventSource` 只能发 GET 且不能带自定义请求头，任务参数塞不进流式请求。
2. **为什么事件不用 SSE 的 `event:` 字段？**
   统一走默认 `message` 事件、按 `data.type` 分派，前端只需一个 handler。
3. **澄清为什么是「暂停」而不是「中断」？**
   服务端协程挂在 `asyncio.Future` 上，状态置为 `awaiting_input`，
   用户回答后由另一个 HTTP 请求 `set_result` 唤醒。同时处理了「答案比 Future 先到」
   的竞态（SSE 推送与 HTTP 提交是两条独立链路）。

### 验收结果

| 检查项 | 结果 |
|---|---|
| 单元测试 | ✅ 225 passed（新增 16 个 Web 用例） |
| 真实启动 + curl | ✅ health / tools / sessions / SSE 全部正常 |
| SSE 事件流（真实 DeepSeek） | ✅ `run_started → turn_started → tool_call → tool_result → plan_ready` 按序到达 |
| 澄清跨请求（真实 DeepSeek） | ✅ 状态转 `awaiting_input` → POST 答案 → 恢复 → 产出 19 步计划 |
| 限流 | ✅ 连续 12 次建会话：前 10 次 200，第 11 次起 429 |
| Swagger 文档 | ✅ `/docs` 返回 200 |
| 既有 209 个测试 | ✅ 全部通过（异步化改造未改变行为） |

### 一个测试踩的坑

`test_clarification_flow_over_http` 最初在 SSE 流的读取循环里**嵌套发新请求**，
导致 httpx 的 ASGI transport 互相等待、测试超时。
改为「两个独立客户端 + `asyncio.gather` 并发」后正常。

> 教训：SSE 流会长时间占住一个连接。测试里不要在同一条流上嵌套请求，
> 用独立客户端并发跑。

---

## 九、前端记录（2026-09-30）

> 对应 Step 3。技术栈：**React 18 + TypeScript + Vite**。

### 交付物

```
frontend/src/
├─ api/types.ts                 前后端契约（事件 + Plan + Trace 的 TS 类型）
├─ api/client.ts                HTTP + SSE 客户端（含游标续传与退避重连）
├─ hooks/usePlanningSession.ts  状态机：提交 → 订阅 → 澄清 → 结果
├─ components/
│  ├─ TaskInput.tsx             输入框 + 示例任务
│  ├─ ProgressFeed.tsx          实时进度流
│  ├─ ClarificationCard.tsx     澄清提问与作答
│  └─ PlanView.tsx              计划时间线（含依赖关系标注）
├─ App.tsx
├─ main.tsx
└─ styles.css
```

构建产物输出到 `web/static/`（161 KB，gzip 后 JS 约 50 KB），由 FastAPI 直接托管 ——
**生产环境只需一个进程，用户不需要 Node 环境**。

### 三个非显然的实现决策

**1. 事件流订阅写在用户操作里，不写在 `useEffect` 里**

React 18 的 `StrictMode` 开发模式下会「挂载 → 卸载 → 再挂载」。
把 `EventSource` 建在 effect 里会开出两条连接、事件重复 —— 这正是 Step 1 提前预警的坑。
改成点击提交时才开流，StrictMode 不会重复执行，不需要 ref 守卫这类补丁。

**2. SSE 断线重连必须自己管游标**

浏览器原生 `EventSource` 自动重连时重发**同一个 URL**，
而服务端的 `?cursor=N` 会过期，导致重复事件。
因此关掉自动重连，自己记录已收到的条数、按退避策略重连并带上新游标；
收到 `plan_ready` / `error` 即视为终态，不再重连。

**3. 类型是契约，不是装饰**

`types.ts` 里的 `PlanEvent` 是可辨识联合，`switch (event.type)` 时 `data` 自动收窄。
后端若改字段，先改这个文件，TS 编译器会把所有受影响的渲染代码指出来，
而不是等到运行时才发现某个字段是 `undefined`。

### 真实浏览器验收（Chromium）

不是「能编译」就算通过，用真实浏览器跑了完整用户流程：

| 步骤 | 结果 |
|---|---|
| 打开页面 | ✅ React 正常渲染（标题、输入框、示例按钮） |
| 输入任务并提交 | ✅ 成功建立会话 |
| **实时进度** | ✅ 逐条出现 `→ 调用 get_weather_forecast` / `✓ 1464ms · 预算 1/12` |
| 产出计划 | ✅ 3 阶段 13 步渲染完整，含工具/依赖/耗时/数据来源标签 |
| **澄清流程** | ✅ 卡片出现（问题 + 原因 + 4 个可点选项） |
| 提交答案后恢复 | ✅ 卡片消失、协程恢复、产出 3 阶段 16 步计划 |

DOM 断言：`clarifyCardExists: false`、`planPanelExists: true`、
`stepCount: 16`、`phaseCount: 3`。

截图见 `docs/screenshots/`。

### 构建与类型检查

```bash
cd frontend
npm run typecheck      # tsc --noEmit → 通过
npm run build          # → ../web/static/
```

`tsconfig.json` 开了 `strict` + `noUnusedLocals` + `noUncheckedIndexedAccess`，
不是宽松配置。

### 遗留

- 前端没有单元测试（只有类型检查 + 真实浏览器验收）。若长期维护，
  建议补 Vitest + React Testing Library 覆盖状态机分支。
- 未做移动端适配（当前按桌面宽度设计）。
- 未做 i18n（界面文案硬编码为中文）。

---

## 十、桌面版记录（2026-09-30）

> 用 pywebview 把现有 Web 界面装进原生窗口。**不打包 exe**，自己用够了。

### 交付物

| 文件 | 说明 |
|---|---|
| `desktop/app.py` | 约 80 行的桌面入口 |
| `start-desktop.bat` | 双击启动（**纯 ASCII 内容**，避免控制台代码页乱码） |
| `pyproject.toml` | 新增 `task-planner-desktop` 入口 |

新依赖：`pywebview 6.2.1`（Windows 后端带 `pythonnet`）。

### 为什么只需要 80 行

架构已经天然适合桌面化，三个条件全部满足：

| 条件 | 状态 |
|---|---|
| 前端是静态产物，运行时不需要 Node | ✅ 构建到 `web/static/` |
| 后端单进程同时提供 API + 静态文件 | ✅ FastAPI 挂载 StaticFiles |
| **不依赖 MCP 子进程** | ✅ Web 层用 `LocalToolRunner` |

所以桌面版 = 启动本地服务 + 开窗口指向它，**不复制任何业务逻辑**。

### 三个实现要点

**1. 端口从已绑定的 socket 读回，而不是「先探测再绑定」**

```python
config = uvicorn.Config(app, host="127.0.0.1", port=0, ...)   # 交给系统分配
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
while not server.started: time.sleep(0.05)
port = server.servers[0].sockets[0].getsockname()[1]          # 读回真实端口
```

「先探测空闲端口再绑定」两步之间有竞态窗口，可能被别的进程抢走。
绑定 0 端口再读回则**完全没有竞态**。

**2. uvicorn 必须跑在后台线程**

pywebview 的窗口循环占用主线程，无法共存于同一线程。

**3. 关窗即退出**

```python
try:
    webview.start()
finally:
    server.should_exit = True    # 不写这句会留下占端口的僵尸进程
```

### 验收方式

本环境的 **PowerShell 工具不返回任何输出**，无法用 `Get-Process MainWindowTitle`
枚举窗口。改用 **ctypes 调 Win32 `EnumWindows`**：

```python
@ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
def cb(hwnd, _):
    if user32.IsWindowVisible(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        titles.append(buf.value)
    return True
```

结果：

| 检查项 | 结果 |
|---|---|
| 窗口存在 | ✅ 枚举到「任务规划助手」 |
| 进程存活 | ✅ 154 MB（GUI 已加载） |
| 内嵌服务 | ✅ `127.0.0.1:57046/api/health` 返回 200 |
| 动态端口 | ✅ 每次启动不同（57046），无冲突 |
| 退出清理 | ✅ 关窗后进程与端口均释放 |

### 顺带发现的环境问题

**`uv run` 会留僵尸进程** —— `TaskStop` 只杀掉 wrapper，
`task-planner-web.exe` 会残留并占着端口和文件句柄，导致下次 `uv sync` 失败
（`failed to remove file ... os error 32`）。需要手动清理：

```bash
taskkill //F //IM task-planner-web.exe
```

**PowerShell 工具在本环境无输出** —— 改用 Bash + `tasklist` / `netstat`。

### 打包成 exe 的前置改造（未做）

| 项 | 现状 | 打包后的问题 |
|---|---|---|
| `OUTPUT_DIR` | 默认 `"outputs"`（相对路径） | 双击启动时 CWD 不确定 → 落盘位置不可控 |
| `attractions.db` | 建在 `PROJECT_ROOT` 下 | onefile 模式下写进临时解压目录，退出即删 |
| API Key | 在 `.env` | 桌面用户不该编辑 `.env`，需设置界面存 `%APPDATA%` |

**还有一个打包后才暴露的坑**：`MCPToolRunner` 用
`sys.executable -m mcp_server.server` 启动子进程，打包后 `sys.executable` 是
`app.exe`，这条命令会重启 GUI 而不是 MCP server。
桌面版走 `LocalToolRunner` 用不到，但保留 CLI 的 MCP 模式时需要加分支处理。

若要做可分发 exe，预估还需 4–6 天（其中 PyInstaller 打包调通占 2–3 天）。

---

## 十一、代码评审缺陷修复记录（2026-10-03）

> 以「资深科研工程评审」视角通读全项目后，修复了评审确认的**高优先级缺陷**。
> 所有缺陷都先用可执行脚本复现（不是「看起来像 bug」），再修，再补回归测试。

### 测试增量

| | 修复前 | 修复后 |
|---|---|---|
| 用例总数 | 225 | **275** |
| 新增文件 | — | `tests/test_fixes.py`（43 例） |
| `test_web.py` | 16 | 23 |

### 一、功能性 Bug（7 个，全部已复现）

**1. `fetch_webpage` 标题永远为空**

- 根因：`head` 在 `SKIP_TAGS` 里，`handle_starttag("head")` 使跳过计数为 1；
  `title` 在 `head` 内，`handle_data` 提前 return，标题文本被丢弃。
- 现象：真实网页（都含 `<head>`）的 `title` 恒为 `"(无标题)"`。
- 修复：`head` 移出跳过集；`title` 单独处理。
- 验证：新增端到端测试 —— 起本地 HTTP 服务返回真实 HTML，断言标题正确解析。

**2. 未闭合 `<script>` 吞掉整页正文**

- 根因：跳过状态用**整数计数**而非栈，畸形 HTML 下永不归零。
- 现象：正文从 `'标题\n正文段落'` 变成 `'标题'`。
- 修复：改用栈；结束时若栈非空，在返回里加 `parse_warning` 标记。
- 验证：`test_skip_stack_survives_malformed_html`、`test_unclosed_script_is_flagged`。

**3. 空计划被判定为成功**

- 根因：`Plan.phases` 默认空列表且无最少步骤校验，`ok` 只看能否解析。
- 现象：`{"goal":"x","phases":[]}` → `ok=True, step_count=0`。
- 修复：`Plan` 加 `model_validator`，要求至少 1 个步骤、`action` 非空。
- 验证：`test_plan_without_steps_is_rejected`、`test_empty_plan_triggers_repair`。

**4. 循环依赖的计划 `ok=True`**

- 根因：`check_dag()` 失败只 `_log`，不参与 `ok` 判定。
- 现象：含环的计划被判为成功，且**评测无法区分合法计划与有环计划**。
- 修复：`PlanResult.ok = plan is not None and trace.dag_ok`；新增
  `trace.dag_ok` / `dag_error` / `output_status`。
- 验证：`test_cyclic_dependency_makes_result_not_ok`。

**5. 重复 step id 被静默去重**

- 根因：`{s.id: s for s in ...}` 后写覆盖前写，无唯一性校验。
- 修复：`model_validator` 校验 id 全局唯一。
- 验证：`test_duplicate_step_ids_are_rejected`。

**6. 预算门控排在缓存之前**

- 根因：`_execute` 先查预算再查缓存，与文档「命中缓存不消耗预算」矛盾。
- 现象：预算耗尽后重复调用同一工具拿不到缓存数据，模型误判「数据缺失」。
- 修复：交换顺序，缓存查找提前。
- 验证：`test_cache_hit_works_after_budget_exhausted`。

**7. 预算拒绝的调用不记入 trace**

- 根因：预算分支直接 return，不 append `ToolCallRecord`。
- 现象：模型请求 2 次、trace 只记 1 条 → **所有基于 trace 的指标系统性偏低**。
- 修复：新增 `ERR_BUDGET_EXCEEDED`，拒绝时也记录。
- 验证：`test_budget_rejection_is_recorded_in_trace`。

### 二、鲁棒性缺陷

| 缺陷 | 修复 |
|---|---|
| `run_stream` 无异常兜底，LLM 报错裸抛 | 包 try/except，产出 `error` 事件 + 失败结果；`stop_reason="runtime_error"` |
| CLI 无异常兜底 | 顶层 try/except + `_error_hint()` 把常见异常翻译成排查提示 |
| LLM 无超时（SDK 默认 600s） | 新增 `LLM_TIMEOUT`（默认 60s）、`LLM_MAX_RETRIES` |
| `choices[0]` 未检查空 | 空列表时抛带说明的 `RuntimeError` |
| `finish_reason="length"` 被忽略 | 产出 `warning` 事件，避免无效的修复重试 |
| `max_turns` 耗尽 + `final_text` 空 → 跳过修复 | 去掉 `and final_text` 条件；新增 `_repair_instruction()` 分情况给指令 |
| `max_turns=0/-1` 静默接受 | `_env_int(minimum=1)` 校验；编排循环里显式兜底为 1 |
| 澄清无超时 | `asyncio.wait_for`，10 分钟超时后降级为「按假设继续」 |
| 前端 `fetch` 无超时 | `AbortSignal.timeout(15s)` + 超时/断网分别给提示 |
| 前端 `answer()` 静默失败 | 改为 dispatch 错误，用户能看到反馈 |

### 三、安全缺陷

**SSRF（`fetch_webpage` 可抓内网 / 云元数据）**

- 修复三层：
  1. `host_is_blocked()` —— 拒绝 localhost / 私有网段 / link-local / 保留地址，
     且**在 DNS 解析之后**校验（防 `evil.example` 解析到 127.0.0.1）
  2. 手动跟随重定向，**每跳重新校验**（防 302 到内网）
  3. 流式读取 + 5MB 上限（防大文件打爆内存）
- 验证：参数化测试覆盖 10 种内网地址；`ALLOW_PRIVATE_URLS=1` 仅用于本地测试。
- **残余风险**：若环境配置了 `HTTP_PROXY`，代理可能把公网域名解析到内网 ——
  这属于代理的信任边界，已在文档注明。

**限流在反代后失效**

- `request.client.host` 在反向代理后是代理 IP，所有人算一个。
- 修复：新增 `WEB_TRUST_PROXY`，开启时按 `X-Forwarded-For` 第一跳限流。
  **默认关闭**（XFF 可伪造）。

### 四、工程与实验完整性

| 缺陷 | 修复 |
|---|---|
| `Settings.tool_timeout/tool_retries` 是死字段 | 用 `contextvars` 注入：编排循环 `with tool_settings(...)` 包住工具调用 |
| `temperature=0.2` 硬编码 | 改为 `LLM_TEMPERATURE` 配置 |
| `trace.turns` 含修复重试，语义不一致 | 修复不再累加 `turns` |
| `stop_reason` 被覆盖，无法作判据 | 拆成 `stop_reason`（怎么结束）+ `output_status`（产出可用性） |
| 工具描述过时（写 12 城，实际 42 城） | `_coverage_note()` 从数据库实时生成 + 测试断言一致性 |
| `max_price` 描述说「元」但比较当地货币 | 描述改为「**当地货币，不是人民币**」+ 测试断言 |
| 会话 TTL 回收不取消后台协程 | `SessionStore.on_evict` 钩子；`app.py` 注入取消逻辑 |
| 服务空闲时 TTL 不生效 | 新增 `_janitor_loop` 后台清理任务 |
| `check_dag` 递归可能栈溢出 | 改迭代式 DFS，1500 步链式依赖测试通过 |
| `covered` 每次查询都算（成功路径也用不到） | 移到失败路径 |

### 五、修复后验证

| 检查项 | 结果 |
|---|---|
| 后端测试 | ✅ 275 passed |
| 前端类型检查 | ✅ `tsc --noEmit` 通过 |
| 前端构建 | ✅ 154 KB（gzip 50 KB） |
| 真实 DeepSeek 端到端 | ✅ `停止=model_finished 输出=ok`，12,676 token |
| SSRF 拦截 | ✅ 169.254.169.254 / 127.0.0.1 / localhost 全部拒绝 |
| 正常公网抓取 | ⚠️ 本机环境 `HTTP_PROXY` 返回 502，改用本地 mock 服务做端到端验证 |

### 六、未修的（下一批）

评审中的以下项**未在本次修复**，属于新增功能而非缺陷：

- 运行记录持久化（`logs/runs.jsonl` + 汇总脚本）—— 实验迭代的度量基础
- 全局 token / 成本熔断
- `messages` 上下文裁剪
- 结构化日志（`_logging` 替代 `print`）
- 前端单元测试（Vitest + RTL）
