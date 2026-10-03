/**
 * 会话状态机：提交任务 → 订阅事件流 → 处理澄清 → 拿到计划。
 *
 * 为什么不用 useEffect 订阅事件流？
 * React 18 的 StrictMode 在开发模式下会「挂载 → 卸载 → 再挂载」，
 * 写在 effect 里的 EventSource 会建立两条连接、事件重复。
 * 这里改成**由用户操作触发**（点击提交时开流），StrictMode 就不会重复执行，
 * 不需要额外加 ref 守卫这类补丁。
 */

import { useCallback, useEffect, useReducer, useRef } from 'react'
import { createSession, openEventStream, submitAnswer } from '../api/client'
import type { StreamHandle } from '../api/client'
import type { ClarificationData, PlanEvent, PlanReadyData } from '../api/types'

export type Phase = 'idle' | 'running' | 'awaiting' | 'done' | 'failed'

export interface SessionState {
  phase: Phase
  sessionId: string | null
  events: PlanEvent[]
  clarification: ClarificationData | null
  result: PlanReadyData | null
  error: string | null
}

const INITIAL: SessionState = {
  phase: 'idle',
  sessionId: null,
  events: [],
  clarification: null,
  result: null,
  error: null,
}

type Action =
  | { type: 'started'; sessionId: string }
  | { type: 'event'; event: PlanEvent }
  | { type: 'failed'; message: string }
  | { type: 'reset' }

function reducer(state: SessionState, action: Action): SessionState {
  switch (action.type) {
    case 'started':
      return { ...INITIAL, phase: 'running', sessionId: action.sessionId }

    case 'event': {
      const { event } = action
      const events = [...state.events, event]

      switch (event.type) {
        case 'clarification':
          return { ...state, events, phase: 'awaiting', clarification: event.data }

        case 'plan_ready':
          return {
            ...state,
            events,
            phase: event.data.ok ? 'done' : 'failed',
            result: event.data,
            clarification: null,
            error: event.data.ok ? null : '模型未能产出合规的计划 JSON。',
          }

        case 'error':
          return { ...state, events, phase: 'failed', error: event.data.message }

        case 'tool_result':
          // 澄清期间工具结果回来了，说明答案已被接受、流程已恢复
          return { ...state, events, phase: 'running', clarification: null }

        default:
          return { ...state, events }
      }
    }

    case 'failed':
      return { ...state, phase: 'failed', error: action.message }

    case 'reset':
      return INITIAL
  }
}

export interface UsePlanningSession {
  state: SessionState
  start: (task: string) => Promise<void>
  answer: (text: string) => Promise<void>
  reset: () => void
}

export function usePlanningSession(): UsePlanningSession {
  const [state, dispatch] = useReducer(reducer, INITIAL)
  const handleRef = useRef<StreamHandle | null>(null)

  const closeStream = useCallback(() => {
    handleRef.current?.close()
    handleRef.current = null
  }, [])

  // 卸载时收尾。因为流是在用户操作时开启的（而非 effect 里），
  // StrictMode 的模拟卸载不会误关连接。
  useEffect(() => closeStream, [closeStream])

  const start = useCallback(
    async (task: string) => {
      closeStream()
      try {
        const created = await createSession(task)
        dispatch({ type: 'started', sessionId: created.session_id })
        handleRef.current = openEventStream(created.session_id, {
          onEvent: (event) => dispatch({ type: 'event', event }),
          onTransportError: (message) => dispatch({ type: 'failed', message }),
        })
      } catch (err) {
        dispatch({
          type: 'failed',
          message: err instanceof Error ? err.message : '提交失败，请稍后重试。',
        })
      }
    },
    [closeStream],
  )

  const answer = useCallback(
    async (text: string) => {
      const sessionId = state.sessionId
      if (sessionId === null) {
        // 不能静默返回 —— 用户点了提交却毫无反应，不知道发生了什么
        dispatch({
          type: 'failed',
          message: '会话已失效，请重新提交任务。',
        })
        return
      }
      try {
        await submitAnswer(sessionId, text)
        // 真正的状态推进由随后的 tool_result 事件驱动，这里不抢先改状态
      } catch (err) {
        // 提交失败必须让用户看见，否则界面会一直停在澄清卡片上
        dispatch({
          type: 'failed',
          message: err instanceof Error ? err.message : '提交答案失败。',
        })
      }
    },
    [state.sessionId],
  )

  const reset = useCallback(() => {
    closeStream()
    dispatch({ type: 'reset' })
  }, [closeStream])

  return { state, start, answer, reset }
}
