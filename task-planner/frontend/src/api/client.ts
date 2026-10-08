/**
 * 后端 API 客户端。
 *
 * 关于 SSE 的两个要点：
 *
 * 1. **断线重连必须带游标。** 服务端保留全部事件，`?cursor=N` 表示"我已经收到 N 条"。
 *    浏览器原生 EventSource 自动重连时会重发同一个 URL，游标会过期，
 *    所以这里关掉自动重连、自己管理重连与游标。
 *
 * 2. **流结束 = 任务结束。** 服务端在会话 finished 后主动关闭流。
 *    此时不能重连，否则会无限循环。收到 plan_ready / error 即视为终态。
 */

import type {
  CreateSessionResponse,
  PlanEvent,
  RevisionResponse,
  RevisionView,
  SessionListResponse,
  SessionView,
  ToolsResponse,
} from './types'

const BASE = '/api'

// 普通请求的超时。没有它的话，服务端挂起时 fetch 会永久 pending，
// 界面一直卡在 loading 且没有任何反馈。
const REQUEST_TIMEOUT_MS = 15000

/**
 * 带状态码的请求错误。
 *
 * 为什么要状态码：有些失败**不是**「操作没成功」，而是「服务端版本不对」。
 * 最典型的是 405 —— 见 `usePlanningSession.revise()`：改了前端但没重启后端时，
 * 新加的接口会落到静态文件挂载点上，返回 405。这时该提示用户重启服务，
 * 而不是把这次修订标成失败。
 */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      ...init,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'TimeoutError') {
      throw new Error('请求超时，请检查网络或稍后重试。')
    }
    throw new Error('无法连接到服务端，请确认后端已启动。')
  }

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      /* 响应不是 JSON，保留状态码 */
    }
    throw new ApiError(detail, resp.status)
  }

  return (await resp.json()) as T
}

export function createSession(task: string): Promise<CreateSessionResponse> {
  return request<CreateSessionResponse>('/sessions', {
    method: 'POST',
    body: JSON.stringify({ task }),
  })
}

export function getSession(sessionId: string): Promise<SessionView> {
  return request<SessionView>(`/sessions/${sessionId}`)
}

/**
 * 「最近」列表。
 *
 * 数据源在服务端（`logs/sessions.db`）而不是 localStorage —— 这样刷新页面、
 * 重启服务、甚至换一个浏览器，看到的历史都是同一份，而且列表里的每一条
 * 都确实还能打开。
 */
export function listSessions(limit = 20): Promise<SessionListResponse> {
  return request<SessionListResponse>(`/sessions?limit=${limit}`)
}

/**
 * 提交一次增量修订。
 *
 * 返回的 `cursor` 很重要：会话的事件是**追加**的，从 0 开始订阅会把
 * 上一版的进度重放一遍（「已查证」列表也会跟着串）。拿这个游标开流，
 * 才只收到本次修订的过程。
 */
export function createRevision(
  sessionId: string,
  feedback: string,
): Promise<RevisionResponse> {
  return request<RevisionResponse>(`/sessions/${sessionId}/revisions`, {
    method: 'POST',
    body: JSON.stringify({ feedback }),
  })
}

/** 取某一版的完整计划。用于版本菜单里切回旧版。 */
export function getRevision(sessionId: string, seq: number): Promise<RevisionView> {
  return request<RevisionView>(`/sessions/${sessionId}/revisions/${seq}`)
}

export function submitAnswer(sessionId: string, answer: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/sessions/${sessionId}/answers`, {
    method: 'POST',
    body: JSON.stringify({ answer }),
  })
}

/** 结束并清理会话。运行中调用即为「取消」。 */
export function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/sessions/${sessionId}`, { method: 'DELETE' })
}

export function listTools(): Promise<ToolsResponse> {
  return request<ToolsResponse>('/tools')
}

// ---------------------------------------------------------------------------
// 事件流
// ---------------------------------------------------------------------------

export interface StreamHandlers {
  onEvent: (event: PlanEvent) => void
  /** 连接层出错（会尝试自动重连，除非已到终态） */
  onTransportError?: (message: string) => void
}

export interface StreamHandle {
  close: () => void
}

/** 终态事件：收到后不再重连。 */
const TERMINAL: ReadonlySet<PlanEvent['type']> = new Set(['plan_ready', 'error'])

const RECONNECT_DELAY_MS = 1500
const MAX_RECONNECTS = 5

export function openEventStream(
  sessionId: string,
  handlers: StreamHandlers,
  startCursor = 0,
): StreamHandle {
  let source: EventSource | null = null
  let cursor = startCursor
  let reconnects = 0
  let closed = false
  let retryTimer: ReturnType<typeof setTimeout> | null = null

  const close = () => {
    closed = true
    if (retryTimer !== null) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
    source?.close()
    source = null
  }

  const connect = () => {
    if (closed) return
    source = new EventSource(`${BASE}/sessions/${sessionId}/events?cursor=${cursor}`)

    source.onmessage = (message) => {
      let event: PlanEvent
      try {
        event = JSON.parse(message.data) as PlanEvent
      } catch {
        return // 心跳等非 JSON 行，忽略
      }

      cursor += 1
      reconnects = 0 // 收到数据即视为连接健康
      handlers.onEvent(event)

      if (TERMINAL.has(event.type)) close()
    }

    source.onerror = () => {
      // 流被服务端正常关闭（任务结束）时会走到这里。
      // 已到终态或主动关闭就不管；否则按退避重连并带上游标续传。
      //
      // readyState 必须在 close() **之前**读 —— close() 会把它置成 CLOSED，
      // 之后就分辨不出「浏览器判定为永久失败」和「我们自己关的」了。
      const readyState = source?.readyState
      source?.close()
      source = null

      if (closed) return

      // 防重入：同一次失败可能触发多次 onerror。不拦的话，
      // 第二个定时器会覆盖第一个的引用 —— 第一个再也清不掉（泄漏），
      // 而且会并发开出两条连接。
      if (retryTimer !== null) return

      // CLOSED 表示浏览器判定为**致命错误**（非 2xx 状态码 / Content-Type 不符），
      // 它自己不会重连。最常见的原因是会话已被删除（404）。
      // 这时重试 5 次只会让用户白等 20 多秒才看到提示。
      // （措辞刻意留了余地：服务重启等场景也会走到这里。）
      if (readyState === EventSource.CLOSED) {
        handlers.onTransportError?.(
          '无法继续接收进度（会话可能已被删除或服务已重启），请刷新页面。',
        )
        close()
        return
      }

      if (reconnects >= MAX_RECONNECTS) {
        handlers.onTransportError?.('与服务端的连接中断，请刷新页面重试。')
        close()
        return
      }
      reconnects += 1
      retryTimer = setTimeout(() => {
        // 必须清空引用：否则下一次 onerror 的防重入守卫会把自己挡住，
        // 表现就是「重连了一次就再也不重连」。
        retryTimer = null
        connect()
      }, RECONNECT_DELAY_MS * reconnects)
    }
  }

  connect()
  return { close }
}
