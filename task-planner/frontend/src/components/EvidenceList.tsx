/**
 * 「已查证」列表 —— 把 Agent 的工具调用翻译成人话。
 *
 * 这一版刻意**不再输出内部实现细节**：
 * 原来的 `轮次≤8 · 工具≤12`、`预算 1/12`、`get_weather_forecast` 这些
 * 是给开发者看的，对用户是噪音。现在显示的是「查了什么、查到了什么」——
 * 摘要由后端各工具自己生成（`ToolSpec.summarize`），因为**只有工具自己
 * 知道哪些字段是重点**。
 *
 * 流式改造的全部价值就在这里：让用户在等待的 30 秒里看到实质进展，
 * 而不是一个转圈。
 */

import type { PlanEvent } from '../api/types'
import { labelOf } from '../lib/toolLabels'

const ERROR_LABELS: Record<string, string> = {
  NOT_FOUND: '没有数据',
  BAD_ARGS: '参数不合法',
  UPSTREAM_TIMEOUT: '上游超时',
  UPSTREAM_ERROR: '上游错误',
  FORBIDDEN_PATH: '路径不允许',
  FILE_ERROR: '文件错误',
  BUDGET_EXCEEDED: '预算用尽',
}

type ProbeStatus = 'running' | 'ok' | 'err'

interface Probe {
  tool: string
  status: ProbeStatus
  summary: string
  errorCode: string | null
}

/** 把事件流压成「已查证」列表。同一工具被调用多次会得到多条。 */
export function buildProbes(events: PlanEvent[]): Probe[] {
  const probes: Probe[] = []
  // 每个工具名一个待配对队列 —— tool_call 与 tool_result 是按顺序配对的
  const pending = new Map<string, number[]>()

  for (const event of events) {
    if (event.type === 'tool_call') {
      const index = probes.length
      probes.push({
        tool: event.data.tool,
        status: 'running',
        summary: '',
        errorCode: null,
      })
      const queue = pending.get(event.data.tool)
      if (queue === undefined) pending.set(event.data.tool, [index])
      else queue.push(index)
      continue
    }

    if (event.type === 'tool_result') {
      const queue = pending.get(event.data.tool)
      const index = queue?.shift()
      if (index === undefined) continue // 结果没有对应的调用（重连重放等），跳过
      const probe = probes[index]
      if (probe === undefined) continue
      probe.status = event.data.ok ? 'ok' : 'err'
      probe.summary = event.data.summary
      probe.errorCode = event.data.error_code
    }
  }

  return probes
}

function statusText(probe: Probe): string {
  if (probe.status === 'running') return '查询中'
  if (probe.status === 'ok') return probe.summary.length > 0 ? '' : '已完成'
  const code = probe.errorCode
  if (code === null) return '未取到'
  return ERROR_LABELS[code] ?? '未取到'
}

interface Props {
  events: PlanEvent[]
  busy: boolean
}

function ProbeRows({ probes }: { probes: Probe[] }) {
  return (
    <>
      {probes.map((probe, index) => {
        const text = statusText(probe)
        return (
          <div className="probe-row" key={`${probe.tool}-${index}`}>
            <span className="probe-what">{labelOf(probe.tool)}</span>
            <span className="probe-out">
              {probe.status === 'err' ? (
                <em>{probe.summary.length > 0 ? probe.summary : '未取到数据'}</em>
              ) : (
                probe.summary || <em>正在查询…</em>
              )}
            </span>
            <span className={`probe-st ${probe.status === 'running' ? 'run' : probe.status}`}>
              {probe.status === 'running' ? <span className="spinner" /> : text}
            </span>
          </div>
        )
      })}
    </>
  )
}

export function EvidenceList({ events, busy }: Props) {
  const probes = buildProbes(events)
  if (probes.length === 0) return null

  const done = probes.filter((p) => p.status === 'ok').length
  const failed = probes.filter((p) => p.status === 'err').length
  const headline =
    failed > 0 ? `查证了 ${probes.length} 项（${failed} 项未取到）` : `查证了 ${probes.length} 项`

  // 跑完后计划才是主角 —— 把查证过程折起来，但保留可追溯性。
  // 跑的过程中展开，因为这时候它是用户唯一能看到的实质进展。
  if (!busy) {
    return (
      <details className="probe probe-folded">
        <summary>{headline}</summary>
        <ProbeRows probes={probes} />
      </details>
    )
  }

  return (
    <div className="probe">
      <div className="probe-head">
        <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1.4" />
          <path d="M6 3.4v3M6 8.2v.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
        已查证 {done} 项
      </div>
      <ProbeRows probes={probes} />
    </div>
  )
}

/** 三段式进度：理解任务 → 查证数据 → 编排计划。 */
export function StageLine({ events, busy }: { events: PlanEvent[]; busy: boolean }) {
  const probes = buildProbes(events)
  const started = events.length > 0
  const anyRunning = probes.some((p) => p.status === 'running')
  const hasProbes = probes.length > 0

  // 当前停在哪一段
  const stage = !started || (!hasProbes && busy) ? 0 : anyRunning ? 1 : busy ? 2 : 2
  const allDone = !busy && started

  const state = (index: number): 'done' | 'now' | '' => {
    if (allDone) return 'done'
    if (index < stage) return 'done'
    if (index === stage) return 'now'
    return ''
  }

  const marks = ['理解任务', '查证数据', '编排计划']

  return (
    <div className="steps-line">
      {marks.map((mark, index) => (
        <span key={mark} style={{ display: 'contents' }}>
          {index > 0 && <span className="sl-bar" />}
          <span className={`sl ${state(index)}`.trim()}>
            <i>✓</i>
            {mark}
          </span>
        </span>
      ))}
    </div>
  )
}
