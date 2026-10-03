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
  error_code: string | null
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

/** 会话状态机：pending → running ⇄ awaiting_input → done | failed */
export type SessionStatus = 'pending' | 'running' | 'awaiting_input' | 'done' | 'failed'

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
