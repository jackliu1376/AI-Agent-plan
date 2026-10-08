/**
 * 前后端契约。
 *
 * 与后端严格对应：
 * - 事件类型 / 字段  ← agent/events.py
 * - Plan 结构        ← agent/schema.py
 * - Trace 结构       ← agent/loop.py::LoopTrace.to_dict
 *
 * 后端有测试断言「事件 data 必须 JSON 可序列化」，因此这里可以放心按字面量建模。
 * 后端若改了字段，先改这个文件，TS 编译器会把所有受影响的渲染代码指出来。
 */

// ---------------------------------------------------------------------------
// 事件
// ---------------------------------------------------------------------------

export type EventType =
  | 'run_started'
  | 'turn_started'
  | 'tool_call'
  | 'tool_result'
  | 'clarification'
  | 'repair'
  | 'warning'
  | 'plan_ready'
  | 'error'

export interface RunStartedData {
  max_turns: number
  tool_budget: number
  injection_flags: number
}

export interface TurnStartedData {
  turn: number
  max_turns: number
}

export interface ToolCallData {
  tool: string
  args: Record<string, unknown>
}

export interface ToolResultData {
  tool: string
  ok: boolean
  /** 人话摘要，由后端各工具自己生成（`ToolSpec.summarize`）。失败时为空串。 */
  summary: string
  error_code: string | null
  /** 失败原因原文。前端在「已查证」列表里显示它 —— 只给错误码的话，
      用户看到的是「参数不合法」这种四字标签，不知道到底哪里不对。 */
  error_message: string | null
  latency_ms: number
  attempts: number
  cached: boolean
  budget_used: number
  budget: number
}

export interface ClarificationData {
  question: string
  options: string[]
  reason: string
}

export interface RepairData {
  reason: string
}

export interface WarningData {
  message: string
}

export interface PlanReadyData {
  ok: boolean
  markdown: string
  raw_text: string
  plan: Plan | null
  trace: Trace
  step_count: number
  /** 修订时模型自己说这次改了哪几处。初版为空串。 */
  revision_summary: string
}

export interface ErrorData {
  message: string
}

/** 可辨识联合：`switch (event.type)` 时 data 会自动收窄。 */
export type PlanEvent =
  | { type: 'run_started'; data: RunStartedData }
  | { type: 'turn_started'; data: TurnStartedData }
  | { type: 'tool_call'; data: ToolCallData }
  | { type: 'tool_result'; data: ToolResultData }
  | { type: 'clarification'; data: ClarificationData }
  | { type: 'repair'; data: RepairData }
  | { type: 'warning'; data: WarningData }
  | { type: 'plan_ready'; data: PlanReadyData }
  | { type: 'error'; data: ErrorData }

// ---------------------------------------------------------------------------
// 计划
// ---------------------------------------------------------------------------

export interface Step {
  id: string
  action: string
  tool: string | null
  tool_args: Record<string, unknown>
  depends_on: string[]
  eta: string
  deliverable: string
  done_when: string
  data_source: string | null
}

export interface Phase {
  name: string
  steps: Step[]
}

export interface Risk {
  risk: string
  mitigation: string
}

export interface Plan {
  goal: string
  assumptions: string[]
  clarifications_needed: string[]
  phases: Phase[]
  risks: Risk[]
  total_eta: string
  budget_estimate: string
  /** 数据时效声明：票价/开放时间/评分等时效性数据的核对时间与变动提示 */
  data_freshness: string
  /** 用户可自行核实信息的官方渠道（不编造网址，只给渠道类型） */
  verification_channels: string[]
}

// ---------------------------------------------------------------------------
// 运行轨迹
// ---------------------------------------------------------------------------

export interface ToolCallRecord {
  tool: string
  args: Record<string, unknown>
  ok: boolean
  cached: boolean
  error_code: string | null
  latency_ms: number
  attempts: number
}

export interface Trace {
  turns: number
  budget: number
  budget_used: number
  budget_exceeded: boolean
  interrupts: number
  repairs: number
  injection_flags: string[]
  stop_reason: string
  tools_used: string[]
  tool_calls: ToolCallRecord[]
  usage: Record<string, number>
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

/** 会话状态机：pending → running ⇄ awaiting_input → done | failed
 *
 *  `interrupted` 是「没跑完就没了」：服务重启或进程崩溃。
 *  单独一个状态是为了如实说明原因，而不是含糊地报「失败」。
 */
export type SessionStatus =
  | 'pending'
  | 'running'
  | 'awaiting_input'
  | 'done'
  | 'failed'
  | 'interrupted'

export interface CreateSessionResponse {
  session_id: string
  status: SessionStatus
  events_url: string
  answer_url: string
}

export interface SessionView {
  session_id: string
  task: string
  status: SessionStatus
  clarification: ClarificationData | null
  result: PlanReadyData | null
  error: string | null
  event_count: number
  /** 完整事件流。恢复一条旧记录时用它渲染「已查证 N 项」。 */
  events: PlanEvent[]
  /** 版本摘要（不含计划正文）。正文走 getRevision()。 */
  revisions: RevisionSummary[]
}

/** 版本菜单里的一行。不含计划正文 —— 正文动辄几十 KB，列表接口不该背它。 */
export interface RevisionSummary {
  seq: number
  /** 产生这一版的用户反馈。初版为空串。 */
  feedback: string
  /** Unix 秒 */
  created_at: number
  ok: boolean
  step_count: number
  revision_summary: string
}

/** 提交一次修订后的响应。`cursor` 是本次运行事件的起始下标。 */
export interface RevisionResponse {
  ok: boolean
  session_id: string
  seq: number
  cursor: number
  events_url: string
}

export interface RevisionView {
  session_id: string
  seq: number
  feedback: string
  created_at: number
  result: PlanReadyData
}

/** 「最近」列表的一行。刻意不含 events / result —— 列表不需要正文。 */
export interface SessionListItem {
  session_id: string
  task: string
  status: SessionStatus
  /** Unix 秒 */
  created_at: number
  updated_at: number
  /** null = 还没有结论（还在跑），与 false（没成功）不同 */
  ok: boolean | null
}

export interface SessionListResponse {
  count: number
  sessions: SessionListItem[]
}

export interface ToolInfo {
  name: string
  description: string
  parameters: Record<string, unknown>
}

export interface ToolsResponse {
  count: number
  tools: ToolInfo[]
}
