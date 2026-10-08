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
  ApiError,
  createRevision,
  createSession,
  deleteSession,
  getRevision,
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
  RevisionSummary,
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
  /** 版本链（不含正文）。最新一版在最后。 */
  revisions: RevisionSummary[]
  /** 当前正在看第几版；0 表示还没有版本 */
  activeSeq: number
  /**
   * 正在离开空状态，以及用哪种节奏离开。
   *
   * - ``'fade'``：用户**开始一件新事**（提交任务）。值得放一段 420ms 的
   *   「启动」手势 —— 那几秒本来就要等模型跑。
   * - ``'fast'``：用户只是**打开一份已有的记录**。计划在服务端现成，
   *   网络往返 ~20ms，此时再放 420ms 动画就是纯粹的等待 ——
   *   实测过：计划 23ms 就渲染好了，却被压在淡出中的光区下面，
   *   用户要等到 446ms 才看得见。
   *
   * 两个值都在**点击的那一刻同步置位**，不等网络回来 ——
   * 否则用户点完会先愣一下，动画才开始。
   */
  leaving: 'none' | 'fade' | 'fast'
  /**
   * 正在修订时回显的用户原话。
   *
   * 有两个用途：界面上让用户看到「我在按这句话改」，以及区分这次运行
   * 是初版还是修订 —— ``plan_ready`` 要靠它决定要不要往版本链里加一项。
   */
  pendingFeedback: string | null
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
  revisions: [],
  activeSeq: 0,
  pendingFeedback: null,
  leaving: 'none',
}

type Action =
  | { type: 'started'; sessionId: string; task: string }
  | { type: 'leaving'; mode: 'fade' | 'fast' }
  | { type: 'revising'; feedback: string }
  | { type: 'event'; event: PlanEvent }
  | { type: 'failed'; message: string }
  | { type: 'reviseFailed'; message: string }
  | { type: 'cancelled'; message: string }
  | { type: 'notice'; message: string }
  | { type: 'dismissError' }
  | { type: 'removed'; sessionId: string }
  | { type: 'showVersion'; seq: number; result: PlanReadyData }
  | { type: 'reset' }
  | { type: 'history'; history: HistoryEntry[] }
  | {
      type: 'restored'
      sessionId: string
      task: string
      result: PlanReadyData | null
      events: PlanEvent[]
      revisions: RevisionSummary[]
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
        // **必须把 leaving 带过来。** INITIAL 里是 'none'，
        // 而 start()/restore() 在**点击那一刻**就把它设成了 fade/fast ——
        // 用 INITIAL 覆盖的话，退场定时器会被清掉，光区永远不卸载，
        // 计划就渲染在那块永不消失的拼贴下面（而且 TaskInput 也不会卸载，
        // 回首页时输入框里还留着上次的文字）。
        leaving: state.leaving,
      }

    case 'history':
      return { ...state, history: action.history }

    case 'leaving':
      return { ...state, leaving: action.mode }

    case 'revising':
      // 进修订：清掉上一次的错误与遗留的澄清，但**保留** events ——
      // 事件是追加的，新一次运行会接着往后写。
      return {
        ...state,
        phase: 'running',
        pendingFeedback: action.feedback,
        clarification: null,
        error: null,
      }

    case 'showVersion': {
      const latest = state.revisions.length
      return {
        ...state,
        // **phase 保持不动。** 它描述的是「这次会话跑得怎么样」，
        // 不是「你正在看的那一版怎么样」。
        //
        // 之前这里写成 `action.result.ok ? 'done' : 'failed'` ——
        // 结果是「点开一版失败的修订看一眼」会把整个会话标成失败，
        // 而失败态的提示条上挂的是「重新开始」按钮（见 App.tsx），
        // 用户只想看看旧版本，一不留神就把手上的计划丢了。
        phase: state.phase,
        result: action.result,
        activeSeq: action.seq,
        // 查证记录属于「最新一次运行」，看旧版时留着它会张冠李戴
        events: action.seq === latest ? state.events : [],
        clarification: null,
        // error 只描述**这一版**：切到一版好的旧版本，提示就该消失。
        error: action.result.ok ? null : '这一版没有产出合规的计划。',
      }
    }

    case 'restored': {
      const ok = action.result !== null && action.result.ok
      const latest = action.revisions.length
      return {
        ...state,
        phase: ok ? 'done' : 'failed',
        sessionId: action.sessionId,
        task: action.task,
        result: action.result,
        events: action.events,
        revisions: action.revisions,
        activeSeq: latest,
        clarification: null,
        error: ok ? null : (action.error ?? '模型未能产出合规的计划 JSON。'),
        pendingFeedback: null,
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
          // 每一次运行（初版也算）在服务端都会登记成一版，本地照做。
          // 只补修订那次的话，本地版本号会比服务端少 1 —— 界面上就会
          // 把刚改完的显示成「第 1 版」，而点开菜单又是「第 2 版」。
          const feedback = state.pendingFeedback ?? ''
          const revisions = [
            ...state.revisions,
            {
              seq: state.revisions.length + 1,
              feedback,
              created_at: Date.now() / 1000,
              ok: event.data.ok,
              step_count: event.data.step_count,
              revision_summary: event.data.revision_summary,
            },
          ]
          return {
            ...state,
            events,
            phase: event.data.ok ? 'done' : 'failed',
            result: event.data,
            clarification: null,
            error: event.data.ok ? null : '模型未能产出合规的计划 JSON。',
            history,
            revisions,
            activeSeq: revisions.length,
            pendingFeedback: null,
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
          return {
            ...state,
            events,
            phase: 'failed',
            error: event.data.message,
            history,
            // 服务端在驱动层出错时不登记版本，本地也不补
            pendingFeedback: null,
          }
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
      return {
        ...state,
        phase: 'failed',
        error: action.message,
        history,
        pendingFeedback: null,
      }
    }

    case 'reviseFailed':
      // 「提交这次修订」这个动作失败了，但**会话本身没坏** ——
      // 上一版计划还在，不该把它标成 failed、也不该往历史里塞一条失败记录。
      // 阶段回到 result 对应的状态，只是多一条提示。
      return {
        ...state,
        phase: state.result === null ? 'failed' : state.result.ok ? 'done' : 'failed',
        error: action.message,
        pendingFeedback: null,
      }

    case 'cancelled':
      // 刻意**不写历史**：取消走的是 DELETE（服务端那一行已经删了），
      // 本地再补一条的话，刷新后它会凭空消失，前后不一致。
      return { ...state, phase: 'failed', error: action.message }

    case 'notice':
      // 只提示，不动阶段也不动历史。
      // 删除失败属于「旁路的旁路」—— 不该把主画布切成 failed。
      return { ...state, error: action.message }

    case 'dismissError':
      return { ...state, error: null }

    case 'removed':
      return {
        ...state,
        history: state.history.filter((item) => item.sessionId !== action.sessionId),
      }

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
  /** 按用户反馈做一次增量修订（调用方负责提供反馈文本） */
  revise: (feedback: string) => Promise<void>
  /** 切到某一版（只影响展示，不改变服务端的「当前版本」） */
  selectVersion: (seq: number) => Promise<void>
  /** 删除一条历史记录（调用方负责先弹确认框） */
  remove: (sessionId: string) => Promise<void>
  /** 关掉提示条（不改变会话状态） */
  dismissError: () => void
  /** 只弹一条提示，不动阶段与历史（给「复制失败」这类旁路反馈用） */
  notify: (message: string) => void
  refreshHistory: () => Promise<void>
  reset: () => void
}

export function usePlanningSession(): UsePlanningSession {
  const [state, dispatch] = useReducer(reducer, INITIAL)
  const handleRef = useRef<StreamHandle | null>(null)
  const sessionRef = useRef<string | null>(null)

  /**
   * 「加载某个会话」的请求序号。每次 start / restore / reset 都自增。
   *
   * 用途：**丢弃过期响应**。快速连点两条历史时两个 `getSession` 会并发，
   * 慢的那个后返回就会覆盖掉用户真正选中的那条 —— 界面内容与侧栏高亮不一致。
   * 每个请求记下自己发起时的序号，回来后比对；不是最新的就直接丢掉。
   *
   * 用 ref 而不是 state：这些比较发生在 await 之后，
   * state 在闭包里会过期，而 ref 永远是最新值。
   */
  const requestSeqRef = useRef(0)

  /**
   * 提交守卫。`start()` 里 `await createSession` 期间 `phase` 还没变、
   * `busy` 仍是 false，输入框的提交按钮**仍然可点** ——
   * 第二次点击会再建一个会话，服务端留下孤儿记录（既烧额度又污染「最近」）。
   *
   * 用 ref 而不是 state：setState 是异步的，等它生效时第二次点击已经进来了。
   */
  const submittingRef = useRef(false)

  // 会话 id 同时存在 ref 里：回调里需要最新值，而 state 在闭包里会过期
  sessionRef.current = state.sessionId

  const closeStream = useCallback(() => {
    handleRef.current?.close()
    handleRef.current = null
  }, [])

  /** 作废所有在途的加载请求（新的加载开始时、以及 reset 时调用）。 */
  const invalidatePendingLoads = useCallback(() => {
    requestSeqRef.current += 1
    return requestSeqRef.current
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
      // 防重复提交：`await createSession` 期间 busy 还是 false，按钮仍可点。
      // 这里同步置位（ref 不是 state），第二次点击会直接被挡回去。
      if (submittingRef.current) return
      submittingRef.current = true

      closeStream()
      const seq = invalidatePendingLoads()
      dispatch({ type: 'leaving', mode: 'fade' })
      try {
        const created = await createSession(task)
        // 等待期间用户可能已经点了别的历史记录 —— 那次操作序号更新，
        // 我们这份结果就作废，否则会把用户选中的内容顶掉。
        if (seq !== requestSeqRef.current) return
        dispatch({ type: 'started', sessionId: created.session_id, task })
        handleRef.current = openEventStream(created.session_id, {
          onEvent: (event) => dispatch({ type: 'event', event }),
          onTransportError: (message) => dispatch({ type: 'failed', message }),
        })
      } catch (err) {
        if (seq !== requestSeqRef.current) return
        dispatch({
          type: 'failed',
          message: err instanceof Error ? err.message : '提交失败，请稍后重试。',
        })
      } finally {
        submittingRef.current = false
      }
    },
    [closeStream, invalidatePendingLoads],
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
      // 序号仲裁：连点两条历史时，慢的那个响应回来后会被丢掉，
      // 不会覆盖用户真正选中的那条。
      const seq = invalidatePendingLoads()
      // 打开已有记录：计划是现成的，别让用户等动画
      dispatch({ type: 'leaving', mode: 'fast' })
      try {
        const view = await getSession(sessionId)
        if (seq !== requestSeqRef.current) return

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
          revisions: view.revisions ?? [],
          // 没产出计划时把服务端的原话带上：可能是「被中断了」，
          // 也可能是「模型输出不合规」—— 两种情况该说的话不一样。
          error: view.error,
        })
      } catch (err) {
        if (seq !== requestSeqRef.current) return
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
    [closeStream, invalidatePendingLoads, refreshHistory],
  )

  /**
   * 增量修订：把上一版计划和这条反馈一起回灌。
   *
   * 订阅时**必须带服务端返回的 cursor** —— 事件是追加的，
   * 从 0 开始会把上一版的进度重放一遍。
   */
  const revise = useCallback(
    async (feedback: string) => {
      const sessionId = sessionRef.current
      if (sessionId === null) {
        dispatch({ type: 'notice', message: '会话已失效，请重新提交任务。' })
        return
      }
      closeStream()
      dispatch({ type: 'revising', feedback })
      try {
        const created = await createRevision(sessionId, feedback)
        handleRef.current = openEventStream(
          sessionId,
          {
            onEvent: (event) => dispatch({ type: 'event', event }),
            onTransportError: (message) => dispatch({ type: 'failed', message }),
          },
          created.cursor,
        )
      } catch (err) {
        // 405 = 请求落到了静态文件挂载点上，说明后端**没有这条路由**。
        // 最常见的原因是改了后端但没重启服务 —— 这时说清楚比抛「Method Not Allowed」
        // 有用得多，那个词只会让人以为是自己用错了 HTTP 方法。
        const stale = err instanceof ApiError && err.status === 405
        dispatch({
          type: 'reviseFailed',
          message: stale
            ? '后端服务还是旧版本（没有修订接口）。请重启后端：停掉 uv run task-planner-web 再重新运行，然后刷新本页。'
            : err instanceof Error
              ? err.message
              : '提交修改失败，请稍后重试。',
        })
      }
    },
    [closeStream],
  )

  const selectVersion = useCallback(
    async (seq: number) => {
      const sessionId = sessionRef.current
      if (sessionId === null) return
      closeStream()
      try {
        const view = await getRevision(sessionId, seq)
        dispatch({ type: 'showVersion', seq, result: view.result })
      } catch (err) {
        dispatch({
          type: 'notice',
          message: err instanceof Error ? `取第 ${seq} 版失败：${err.message}` : '取历史版本失败。',
        })
      }
    },
    [closeStream],
  )

  const reset = useCallback(() => {
    closeStream()
    // 作废在途的加载：否则「点历史 → 立刻返回首页」时，
    // 那个还没回来的 getSession 会把首页又顶成旧会话。
    invalidatePendingLoads()
    dispatch({ type: 'reset' })
  }, [closeStream, invalidatePendingLoads])

  const dismissError = useCallback(() => {
    dispatch({ type: 'dismissError' })
  }, [])

  /**
   * 只弹一条提示，不动阶段、不动历史。
   *
   * 给调用方用的「旁路反馈」入口 —— 典型场景是复制失败：
   * 那既不改变会话状态，也不该把主画布切成 failed，
   * 但用户**必须**知道没复制成功，否则他会以为剪贴板里已经有内容了。
   */
  const notify = useCallback((message: string) => {
    dispatch({ type: 'notice', message })
  }, [])

  /**
   * 删除一条历史记录。**不做二次确认** —— 那是调用方（弹窗）的事，
   * 这样这个 hook 保持纯粹，也方便别处复用。
   *
   * 采用**乐观更新**：本地删一行只要几毫秒，等一个网络往返再更新界面
   * 只会让人觉得卡。代价是失败时那一行会「弹回来」，所以失败后
   * 必须重新拉一次列表 —— 宁可列表闪一下，也不能让界面和磁盘不一致。
   */
  const remove = useCallback(
    async (sessionId: string) => {
      const wasActive = sessionId === sessionRef.current

      dispatch({ type: 'removed', sessionId })
      if (wasActive) {
        // 删的正是当前打开的那条：主画布要一起收掉。
        // 不收的话你会看着一个已经不存在的记录，复制 Markdown 还会成功。
        closeStream()
        // 同理作废在途加载：正在恢复这条时把它删掉，
        // 那个响应回来会把它又画回主画布。
        invalidatePendingLoads()
        dispatch({ type: 'reset' })
      }

      try {
        await deleteSession(sessionId)
      } catch {
        dispatch({
          type: 'notice',
          message: '删除失败，已把列表恢复回来。请确认后端服务是否在运行。',
        })
        void refreshHistory()
      }
    },
    [closeStream, refreshHistory],
  )

  return {
    state,
    start,
    answer,
    cancel,
    restore,
    revise,
    selectVersion,
    remove,
    dismissError,
    notify,
    refreshHistory,
    reset,
  }
}
