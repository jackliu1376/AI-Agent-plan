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

/**
 * 澄清工具。
 *
 * 它不是「查证」——调用它会挂起整轮运行、等用户回话。
 * 列表里的措辞要区别对待：显示「正在查询…」会让人以为工具卡住了，
 * 而实际上**球在用户这边**。
 */
const CLARIFY_TOOL = 'ask_user_clarification'

const ERROR_LABELS: Record<string, string> = {
  NOT_FOUND: '没有数据',
  BAD_ARGS: '参数不合法',
  /** 参数没错，是取值超出了数据源覆盖范围（如查 40 天后的天气）。 */
  OUT_OF_RANGE: '超出数据范围',
  UPSTREAM_TIMEOUT: '上游超时',
  UPSTREAM_ERROR: '上游错误',
  FORBIDDEN_PATH: '路径不允许',
  FILE_ERROR: '文件错误',
  BUDGET_EXCEEDED: '预算用尽',
}

/**
 * 把错误原文压到列表能放下的长度。
 *
 * 后端的消息往往很长（「请求的日期超出预报范围：2026-11-14 ~ 2026-11-18。
 * Open-Meteo 当前只提供 2026-07-07 ~ 2026-10-23 的预报，请改用该区间内的
 * 日期；更远的日期只能给出气候意义上的经验判断…」），塞进一行会把列表撑散。
 * 取第一句 + 截断，完整内容放在 `title` 里。
 */
function shortError(message: string, max = 46): string {
  const first = message.split(/[。\n]/)[0] ?? message
  return first.length > max ? `${first.slice(0, max)}…` : first
}

type ProbeStatus = 'running' | 'ok' | 'err'

interface Probe {
  tool: string
  status: ProbeStatus
  summary: string
  errorCode: string | null
  /**
   * 这次调用查的是什么（城市名 / 路线 / 网址）。
   *
   * 失败时必须显示出来 —— 否则用户看到的是「天气：未取到数据」，
   * 分不清是**工具坏了**还是**这个城市没数据**。实测中用户看到
   * 「查证了 10 项（4 项未取到）」时的第一反应是「为什么很多工具取不到数据」，
   * 而真实情况是其中 4 项都在查同一个未收录的目的地。
   * 带上主语之后，这一行会读作「天气 · 稻城：没有数据」—— 一眼看出是覆盖问题。
   */
  subject: string
  /** 失败原因原文。只给错误码的话，用户看到的是「参数不合法」这种标签，
      不知道到底哪里不对 —— 而后端其实写了可执行的说明。 */
  errorMessage: string | null
}

/** 从工具参数里抽出「查的是什么」。认不出来就返回空串（不硬凑）。 */
function subjectOf(args: Record<string, unknown>): string {
  const str = (v: unknown): string => (typeof v === 'string' ? v : '')

  const city = str(args.city)
  if (city) {
    // 有些工具同时带 city 和具体对象名（实时核对是「城市 + 景点名」）
    const name = str(args.name)
    return name ? `${city} · ${name}` : city
  }

  const origin = str(args.origin)
  const destination = str(args.destination)
  if (origin && destination) return `${origin} → ${destination}`

  return str(args.url) || str(args.path) || ''
}

/** 把事件流压成「已查证」列表。同一工具被调用多次会得到多条。 */
export function buildProbes(events: PlanEvent[]): Probe[] {
  let probes: Probe[] = []
  // 每个工具名一个待配对队列 —— tool_call 与 tool_result 是按顺序配对的
  let pending = new Map<string, number[]>()

  for (const event of events) {
    if (event.type === 'run_started') {
      // 一次会话可能有多次运行（初版 + 若干次修订），事件是追加的。
      // 遇到新的 run_started 就清空 —— 否则修订完成后会显示两次运行的
      // 查证记录，用户以为工具被重复调用了。
      probes = []
      pending = new Map()
      continue
    }

    if (event.type === 'tool_call') {
      const index = probes.length
      probes.push({
        tool: event.data.tool,
        status: 'running',
        summary: '',
        errorCode: null,
        subject: subjectOf(event.data.args ?? {}),
        errorMessage: null,
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
      probe.errorMessage = event.data.error_message ?? null
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
  /**
   * 已经解析好的查证记录。
   *
   * **由调用方算好传进来，不在这里自己算。** `buildProbes` 是 O(events)，
   * 而 `StageLine` 也要用同一份结果 —— 各自算一遍就是白跑两趟，
   * 而且流式期间每来一个事件都会重算。
   */
  probes: Probe[]
  busy: boolean
}

function ProbeRows({ probes }: { probes: Probe[] }) {
  return (
    <>
      {probes.map((probe, index) => {
        const text = statusText(probe)
        return (
          <div className="probe-row" key={`${probe.tool}-${index}`}>
            <span className="probe-what">
              {labelOf(probe.tool)}
              {probe.subject.length > 0 && (
                <span className="probe-subj" title={probe.subject}>
                  {probe.subject}
                </span>
              )}
            </span>
            <span className="probe-out">
              {probe.status === 'err' ? (
                probe.errorMessage !== null ? (
                  <em className="probe-err" title={probe.errorMessage}>
                    {shortError(probe.errorMessage)}
                  </em>
                ) : (
                  <em>{probe.summary.length > 0 ? probe.summary : '未取到数据'}</em>
                )
              ) : (
                probe.summary || (
                  <em>
                    {probe.tool === CLARIFY_TOOL ? '等待你的回答…' : '正在查询…'}
                  </em>
                )
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

export function EvidenceList({ probes, busy }: Props) {
  if (probes.length === 0) return null

  const done = probes.filter((p) => p.status === 'ok').length
  const failed = probes.filter((p) => p.status === 'err').length
  const headline =
    failed > 0 ? `查证了 ${probes.length} 项（${failed} 项未取到）` : `查证了 ${probes.length} 项`

  // 跑着的时候的标题。
  //
  // 原来固定写「已查证 N 项」，N 只数已完成的 —— 所以刚提交、一个都还没回来时
  // 会显示「已查证 0 项」，读起来像出错了。改成按情况说话：
  // 在等用户回答 → 说「等待你的回答」；还在跑 → 说「正在查证…」。
  const waiting = probes.some((p) => p.status === 'running' && p.tool === CLARIFY_TOOL)
  const runningHead = waiting ? '等待你的回答' : done === 0 ? '正在查证…' : `已查证 ${done} 项`

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
        {runningHead}
      </div>
      <ProbeRows probes={probes} />
    </div>
  )
}

/** 三段式进度：理解任务 → 查证数据 → 编排计划。
 *
 * **修订时不走查证那一段。** 模型直接基于已有信息改计划，
 * 事件流只有 `run_started → turn_started → plan_ready`，**全程没有 tool_call**
 * （实测确认）。而按「有没有查证」判阶段的旧逻辑会因此永远停在第一步 ——
 * 用户看到的就是「阶段条不动，等一会儿新计划直接蹦出来」。
 *
 * 所以修订单独走两步：理解反馈 → 编排计划。
 */
export function StageLine({
  probes,
  eventCount,
  turned,
  busy,
  revising = false,
}: {
  /** 与 EvidenceList 共用同一份（由 App 算好），不在这里重算。 */
  probes: Probe[]
  /** 事件总数。只用它判断「有没有开始」，不遍历数组。 */
  eventCount: number
  /** 是否已经出现过 turn_started。修订时用它把第一段推过去。 */
  turned: boolean
  busy: boolean
  revising?: boolean
}) {
  const started = eventCount > 0
  const anyRunning = probes.some((p) => p.status === 'running')
  const hasProbes = probes.length > 0

  const marks = revising
    ? ['理解反馈', '编排计划']
    : ['理解任务', '查证数据', '编排计划']

  const stage = revising
    ? turned
      ? 1
      : 0
    : !started || (!hasProbes && busy)
      ? 0
      : anyRunning
        ? 1
        : 2
  const allDone = !busy && started

  const state = (index: number): 'done' | 'now' | '' => {
    if (allDone) return 'done'
    if (index < stage) return 'done'
    if (index === stage) return 'now'
    return ''
  }

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
