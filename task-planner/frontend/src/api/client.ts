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
  SessionView,
  ToolsResponse,
} from './types'

const BASE = '/api'

// 普通请求的超时。没有它的话，服务端挂起时 fetch 会永久 pending，
// 界面一直卡在 loading 且没有任何反馈。
const REQUEST_TIMEOUT_MS = 15000

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
    throw new Error(detail)
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

export function submitAnswer(sessionId: string, answer: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/sessions/${sessionId}/answers`, {
    method: 'POST',
    body: JSON.stringify({ answer }),
  })
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

export function openEventStream(sessionId: string, handlers: StreamHandlers): StreamHandle {
  let source: EventSource | null = null
  let cursor = 0
  let reconnects = 0
  let closed = false
  let retryTimer: ReturnType<typeof setTimeout> | null = null

  const close = () => {
    closed = true
    if (retryTimer !== null) clearTimeout(retryTimer)
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
      source?.close()
      source = null
      if (closed) return

      if (reconnects >= MAX_RECONNECTS) {
        handlers.onTransportError?.('与服务端的连接中断，请刷新页面重试。')
        close()
        return
      }
      reconnects += 1
      retryTimer = setTimeout(connect, RECONNECT_DELAY_MS * reconnects)
    }
  }

  connect()
  return { close }
}
