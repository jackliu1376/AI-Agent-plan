/**
 * 实时进度流。
 *
 * 这里就是「事件流」改造的收益所在 —— 用户能看到 Agent 正在查什么、
 * 拿到了什么、花了多久，而不是盯着一个转圈等 30 秒。
 */

import type { PlanEvent } from '../api/types'

interface Line {
  icon: string
  text: string
  meta?: string
  tone: 'plain' | 'ok' | 'err' | 'strong'
}

function describe(event: PlanEvent): Line {
  switch (event.type) {
    case 'run_started':
      return {
        icon: '▶',
        text: '开始规划',
        meta: `轮次≤${event.data.max_turns} · 工具≤${event.data.tool_budget}`,
        tone: 'plain',
      }

    case 'turn_started':
      return {
        icon: '◆',
        text: `第 ${event.data.turn} 轮：请求模型`,
        tone: 'plain',
      }

    case 'tool_call':
      return {
        icon: '→',
        text: `调用 ${event.data.tool}`,
        meta: Object.keys(event.data.args).length > 0 ? '…' : undefined,
        tone: 'plain',
      }

    case 'tool_result':
      return {
        icon: event.data.ok ? '✓' : '✕',
        text: `${event.data.tool}${event.data.cached ? '（缓存）' : ''}${
          event.data.ok ? '' : ` · ${event.data.error_code ?? '失败'}`
        }`,
        meta: `${event.data.latency_ms}ms · 预算 ${event.data.budget_used}/${event.data.budget}`,
        tone: event.data.ok ? 'ok' : 'err',
      }

    case 'clarification':
      return { icon: '?', text: '需要你补充信息', tone: 'strong' }

    case 'repair':
      return {
        icon: '!',
        text: '输出不合规，正在重试',
        meta: event.data.reason.slice(0, 30),
        tone: 'plain',
      }

    case 'warning':
      return {
        icon: '!',
        text: event.data.message,
        tone: 'err',
      }

    case 'plan_ready':
      return {
        icon: '✔',
        text: event.data.ok ? '计划已生成' : '未能生成合规计划',
        meta: `${event.data.step_count} 个步骤`,
        tone: event.data.ok ? 'ok' : 'err',
      }

    case 'error':
      return { icon: '✕', text: event.data.message, tone: 'err' }
  }
}

interface Props {
  events: PlanEvent[]
  busy: boolean
}

export function ProgressFeed({ events, busy }: Props) {
  if (events.length === 0) return null

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>进度</h2>
        {busy && <span className="spinner" aria-label="进行中" />}
      </div>
      <div className="panel-body">
        <div className="feed" role="log" aria-live="polite">
          {events.map((event, index) => {
            const line = describe(event)
            return (
              <div key={index} className={`feed-line ${line.tone}`}>
                <span className="feed-icon">{line.icon}</span>
                <span>{line.text}</span>
                {line.meta !== undefined && <span className="feed-meta">{line.meta}</span>}
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}
