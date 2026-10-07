/**
 * 后端健康状态 + 模型名。
 *
 * 为什么需要它：侧栏底部原来硬编码「数据源已就绪」，**后端挂了也照样显示已就绪**。
 * 用户看到的是「一切正常」，然后提交任务时才报错 —— 排查方向完全被误导。
 *
 * 顺带把 `/api/health` 返回的模型名带出来：输入框的工具条上要显示它。
 * 单独再开一个 hook 轮询同一个接口是浪费，而且两次请求之间模型名可能不一致。
 *
 * 轮询间隔 20 秒：请求很轻（一个 JSON），但要足够快地在后端重启后自动恢复显示。
 */

import { useEffect, useState } from 'react'

export type BackendStatus = 'checking' | 'online' | 'offline'

export interface BackendInfo {
  status: BackendStatus
  /** 后端实际使用的模型名；未连上时为空串 */
  model: string
}

const POLL_MS = 20000
const TIMEOUT_MS = 4000

const UNKNOWN: BackendInfo = { status: 'checking', model: '' }

export function useBackendHealth(): BackendInfo {
  const [info, setInfo] = useState<BackendInfo>(UNKNOWN)

  useEffect(() => {
    let cancelled = false

    const check = async () => {
      try {
        const resp = await fetch('/api/health', {
          signal: AbortSignal.timeout(TIMEOUT_MS),
        })
        if (cancelled) return
        if (!resp.ok) {
          setInfo({ status: 'offline', model: '' })
          return
        }
        const body: unknown = await resp.json()
        const model =
          typeof body === 'object' && body !== null && typeof (body as { model?: unknown }).model === 'string'
            ? (body as { model: string }).model
            : ''
        if (!cancelled) setInfo({ status: 'online', model })
      } catch {
        // 超时 / 连接被拒 / 代理失败都归为「连不上」
        if (!cancelled) setInfo({ status: 'offline', model: '' })
      }
    }

    void check()
    const timer = setInterval(() => void check(), POLL_MS)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  return info
}
