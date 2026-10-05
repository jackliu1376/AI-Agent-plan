/**
 * 会话状态机：提交任务 → 订阅事件流 → 处理澄清 → 拿到计划。
 *
 * 四个设计决定：
 *
 * 1. **事件流订阅由用户操作触发，不写在 useEffect 里。**
 *    React 18 的 StrictMode 开发模式下会「挂载 → 卸载 → 再挂载」，
 *    写在 effect 里的 EventSource 会建立两条连接、事件重复。
 *
 * 2. **历史以服务端为准，localStorage 只当缓存。**
 *    服务端把会话落盘（`logs/sessions.db`），因此「列表里的每一条都打得开」
 *    这个承诺才成立。localStorage 的作用只是让首屏（健康检查还没回来时）
 *    不至于空着。反过来的话 —— 本地列表指向的会话可能早就没了。
 *
 * 3. **恢复一条旧记录时连事件一起恢复。**
 *    只给计划不给过程的话，恢复出来的页面会比刚跑完时少一块「已查证」，
 *    看起来像丢东西。
 *
 * 4. **运行中可以取消。** 一次规划 15–30 秒，期间只能干等是不合理的。
 */

import { useCallback, useEffect, useReducer, useRef } from 'react'
import {
  createSession,
  deleteSession,
  getSession,
  listSessions,
  openEventStream,
  submitAnswer,
} from '../api/client'
import type { StreamHandle } from '../api/client'
import type {
  ClarificationData,
  PlanEvent,
  PlanReadyData,
  SessionStatus,
} from '../api/types'

export type Phase = 'idle' | 'running' | 'awaiting' | 'done' | 'failed'

/** 一条历史记录。列表要的东西就这些，正文按需回捞。 */
export interface HistoryEntry {
  sessionId: string
  task: string
  /** Unix 毫秒 */
  ts: number
  /** null = 还没有结论（还在跑） */
  ok: boolean | null
  status: SessionStatus
}

export interface SessionState {
  phase: Phase
  sessionId: string | null
  /** 当前任务的原文，用于顶部回显 */
  task: string
  events: PlanEvent[]
  clarification: ClarificationData | null
  result: PlanReadyData | null
  error: string | null
  history: HistoryEntry[]
}

const HISTORY_KEY = 'cairn.history.v2'
/** v1 只存了「列表」，会话体在服务端内存里，重启就点不开。换掉时顺手清掉。 */
const LEGACY_HISTORY_KEY = 'cairn.history.v1'
const HISTORY_MAX = 20

const VALID_STATUS: ReadonlySet<string> = new Set([
  'pending',
  'running',
  'awaiting_input',
  'done',
  'failed',
  'interrupted',
])

/** 还在跑的状态。点开这类记录应该是「接回去」，而不是「读一份结果」。 */
const LIVE_STATUS: ReadonlySet<SessionStatus> = new Set<SessionStatus>([
  'pending',
  'running',
  'awaiting_input',
])

/**
 * 读本地缓存。**这一步是纯兜底** —— 服务端列表才是权威。
 *
 * 兼容 v1 的旧条目（只有 ok，没有 status）：能推断就推断，
 * 推不出来就丢掉。宁可少一条，也不要显示一条点不开的。
 */
function loadHistory(): HistoryEntry[] {
  try {
    localStorage.removeItem(LEGACY_HISTORY_KEY)
    const raw = localStorage.getItem(HISTORY_KEY)
    if (raw === null) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.flatMap((item): HistoryEntry[] => {
      if (typeof item !== 'object' || item === null) return []
      const entry = item as Partial<HistoryEntry>
      if (typeof entry.sessionId !== 'string' || typeof entry.task !== 'string') return []
      const status: SessionStatus =
        typeof entry.status === 'string' && VALID_STATUS.has(entry.status)
          ? (entry.status as SessionStatus)
          : entry.ok === true
            ? 'done'
            : 'failed'
      return [
        {
          sessionId: entry.sessionId,
          task: entry.task,
          ts: typeof entry.ts === 'number' ? entry.ts : 0,
          ok: typeof entry.ok === 'boolean' ? entry.ok : null,
          status,
        },
      ]
    })
  } catch {
    // 隐私模式 / 配额满 / 脏数据：缓存只是锦上添花，读不到就从空开始
    return []
  }
}

function saveHistory(history: HistoryEntry[]): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, HISTORY_MAX)))
  } catch {
    /* 写不进去不影响主流程 */
  }
}

function upsertHistory(history: HistoryEntry[], entry: HistoryEntry): HistoryEntry[] {
  const rest = history.filter((item) => item.sessionId !== entry.sessionId)
  return [entry, ...rest].slice(0, HISTORY_MAX)
}

const INITIAL: SessionState = {
  phase: 'idle',
  sessionId: null,
  task: '',
  events: [],
  clarification: null,
  result: null,
  error: null,
  history: [],
}

type Action =
  | { type: 'started'; sessionId: string; task: string }
  | { type: 'event'; event: PlanEvent }
  | { type: 'failed'; message: string }
  | { type: 'cancelled'; message: string }
  | { type: 'reset' }
  | { type: 'history'; history: HistoryEntry[] }
  | {
      type: 'restored'
      sessionId: string
      task: string
      result: PlanReadyData | null
      events: PlanEvent[]
      error: string | null
    }

function reducer(state: SessionState, action: Action): SessionState {
  switch (action.type) {
    case 'started':
      return {
        ...INITIAL,
        phase: 'running',
        sessionId: action.sessionId,
        task: action.task,
        history: state.history,
      }

    case 'history':
      return { ...state, history: action.history }

    case 'restored': {
      const ok = action.result !== null && action.result.ok
      return {
        ...state,
        phase: ok ? 'done' : 'failed',
        sessionId: action.sessionId,
        task: action.task,
        result: action.result,
        events: action.events,
        clarification: null,
        error: ok ? null : (action.error ?? '模型未能产出合规的计划 JSON。'),
      }
    }

    case 'event': {
      const { event } = action
      const events = [...state.events, event]

      switch (event.type) {
        case 'clarification':
          return { ...state, events, phase: 'awaiting', clarification: event.data }

        case 'plan_ready': {
          const history = upsertHistory(state.history, {
            sessionId: state.sessionId ?? '',
            task: state.task,
            ts: Date.now(),
            ok: event.data.ok,
            status: event.data.ok ? 'done' : 'failed',
          })
          return {
            ...state,
            events,
            phase: event.data.ok ? 'done' : 'failed',
            result: event.data,
            clarification: null,
            error: event.data.ok ? null : '模型未能产出合规的计划 JSON。',
            history,
          }
        }

        case 'error': {
          const history = upsertHistory(state.history, {
            sessionId: state.sessionId ?? '',
            task: state.task,
            ts: Date.now(),
            ok: false,
            status: 'failed',
          })
          return { ...state, events, phase: 'failed', error: event.data.message, history }
        }

        case 'tool_result':
          // 澄清期间工具结果回来了，说明答案已被接受、流程已恢复
          return { ...state, events, phase: 'running', clarification: null }

        default:
          return { ...state, events }
      }
    }

    case 'failed': {
      const history = upsertHistory(state.history, {
        sessionId: state.sessionId ?? '',
        task: state.task,
        ts: Date.now(),
        ok: false,
        status: 'failed',
      })
      return { ...state, phase: 'failed', error: action.message, history }
    }

    case 'cancelled':
      // 刻意**不写历史**：取消走的是 DELETE（服务端那一行已经删了），
      // 本地再补一条的话，刷新后它会凭空消失，前后不一致。
      return { ...state, phase: 'failed', error: action.message }

    case 'reset':
      return { ...INITIAL, history: state.history }
  }
}

export interface UsePlanningSession {
  state: SessionState
  start: (task: string) => Promise<void>
  answer: (text: string) => Promise<void>
  cancel: () => Promise<void>
  restore: (sessionId: string) => Promise<void>
  refreshHistory: () => Promise<void>
  reset: () => void
}

export function usePlanningSession(): UsePlanningSession {
  const [state, dispatch] = useReducer(reducer, INITIAL)
  const handleRef = useRef<StreamHandle | null>(null)
  const sessionRef = useRef<string | null>(null)

  // 会话 id 同时存在 ref 里：回调里需要最新值，而 state 在闭包里会过期
  sessionRef.current = state.sessionId

  const closeStream = useCallback(() => {
    handleRef.current?.close()
    handleRef.current = null
  }, [])

  // 首屏先用本地缓存铺上，别让侧栏空着等网络
  useEffect(() => {
    dispatch({ type: 'history', history: loadHistory() })
  }, [])

  // 历史变化就落盘（首次的空数组不写，避免覆盖掉已有记录）
  useEffect(() => {
    if (state.history.length > 0) saveHistory(state.history)
  }, [state.history])

  // 卸载时收尾。因为流是在用户操作时开启的（而非 effect 里），
  // StrictMode 的模拟卸载不会误关连接。
  useEffect(() => closeStream, [closeStream])

  /** 从服务端拉「最近」。失败就静默保留缓存 —— 离线时不该把列表清空。 */
  const refreshHistory = useCallback(async () => {
    try {
      const body = await listSessions(HISTORY_MAX)
      dispatch({
        type: 'history',
        history: body.sessions.map((item) => ({
          sessionId: item.session_id,
          task: item.task,
          ts: item.created_at * 1000,
          ok: item.ok,
          status: item.status,
        })),
      })
    } catch {
      /* 后端连不上：保持缓存不动，侧栏底部会显示「未连接到服务端」 */
    }
  }, [])

  const start = useCallback(
    async (task: string) => {
      closeStream()
      try {
        const created = await createSession(task)
        dispatch({ type: 'started', sessionId: created.session_id, task })
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
        dispatch({ type: 'failed', message: '会话已失效，请重新提交任务。' })
        return
      }
      try {
        await submitAnswer(sessionId, text)
        // 真正的状态推进由随后的 tool_result 事件驱动，这里不抢先改状态
      } catch (err) {
        dispatch({
          type: 'failed',
          message: err instanceof Error ? err.message : '提交答案失败。',
        })
      }
    },
    [state.sessionId],
  )

  const cancel = useCallback(async () => {
    const sessionId = sessionRef.current
    closeStream()
    if (sessionId !== null) {
      try {
        await deleteSession(sessionId)
      } catch {
        // 服务端可能已经跑完并回收了会话 —— 本地照样收尾，不要卡住界面
      }
    }
    dispatch({ type: 'cancelled', message: '已取消本次规划。' })
  }, [closeStream])

  const restore = useCallback(
    async (sessionId: string) => {
      closeStream()
      try {
        const view = await getSession(sessionId)

        // 还在跑：重新接上事件流，而不是当成一条失败的记录。
        // 服务端保留着全部事件，`?cursor=0` 会从头重放，
        // 所以刷新页面 / 换个浏览器都能接着看同一场运行。
        if (LIVE_STATUS.has(view.status)) {
          dispatch({ type: 'started', sessionId, task: view.task })
          handleRef.current = openEventStream(sessionId, {
            onEvent: (event) => dispatch({ type: 'event', event }),
            onTransportError: (message) => dispatch({ type: 'failed', message }),
          })
          return
        }

        dispatch({
          type: 'restored',
          sessionId,
          task: view.task,
          result: view.result,
          // 事件一起恢复，否则「已查证」那块会消失
          events: view.events ?? [],
          // 没产出计划时把服务端的原话带上：可能是「被中断了」，
          // 也可能是「模型输出不合规」—— 两种情况该说的话不一样。
          error: view.error,
        })
      } catch (err) {
        dispatch({
          type: 'failed',
          message:
            err instanceof Error
              ? `无法恢复这条记录：${err.message}`
              : '无法恢复这条记录。',
        })
        // 列表里可能还留着一条已经不存在（或已过期）的记录，顺手刷新掉
        void refreshHistory()
      }
    },
    [closeStream, refreshHistory],
  )

  const reset = useCallback(() => {
    closeStream()
    dispatch({ type: 'reset' })
  }, [closeStream])

  return { state, start, answer, cancel, restore, refreshHistory, reset }
}
