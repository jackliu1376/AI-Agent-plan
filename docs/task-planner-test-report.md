# 任务规划助手 · 评测报告

> 运行命令：`uv run pytest`
> 结果：**193 passed in 59.43s** ｜ 通过率 **100%** ｜ 更新日期 2026-09-29

---

## 一、总体结果

| 测试文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `tests/test_cities.py` | 116 | 城市解析三层策略、景点库覆盖、早期错误解析的回归用例 |
| `tests/test_tools.py` | 28 | 8 个工具的行为、错误码、沙箱、重试、幂等、Schema 合规 |
| `tests/test_loop.py` | 27 | 编排循环 + 全部护栏（TC-01 ~ TC-10） |
| `tests/test_config.py` | 18 | base_url 校验、系统提示词日期注入、密钥泄漏扫描 |
| `tests/test_mcp_bridge.py` | 4 | MCP 协议桥接（真实拉起子进程，stdio/JSON-RPC 往返） |
| **合计** | **193** | **全部通过** |

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
uv run pytest            # 193 passed
uv run pytest -v         # 查看每个用例名
```

端到端人工验证：

```bash
uv run task-planner --demo -v                      # 离线全链路（真实工具）
uv run task-planner --demo --transport mcp -v      # 走 MCP 协议
uv run task-planner "我想去旅行" -v                  # 真实 DeepSeek（需 Key）
uv run task-planner "我想这周末去纽约玩两天，两个人" -v  # 国际行程
uv run task-planner "帮我规划这周末去伊斯坦布尔玩三天" -v  # 景点库未覆盖时的降级
uv run task-planner "规划这周末去哈尔滨玩三天，带小孩" -v  # 新增省会 + 季节性数据
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
