/**
 * 后端健康状态。
 *
 * 为什么需要它：侧栏底部原来硬编码「数据源已就绪」，**后端挂了也照样显示已就绪**。
 * 用户看到的是「一切正常」，然后提交任务时才报错 —— 排查方向完全被误导。
 *
 * 轮询间隔 20 秒：请求很轻（一个 JSON），但要足够快地在后端重启后自动恢复显示。
 */

import { useEffect, useState } from 'react'

export type BackendStatus = 'checking' | 'online' | 'offline'

const POLL_MS = 20000
const TIMEOUT_MS = 4000

export function useBackendHealth(): BackendStatus {
  const [status, setStatus] = useState<BackendStatus>('checking')

  useEffect(() => {
    let cancelled = false

    const check = async () => {
      try {
        const resp = await fetch('/api/health', {
          signal: AbortSignal.timeout(TIMEOUT_MS),
        })
        if (!cancelled) setStatus(resp.ok ? 'online' : 'offline')
      } catch {
        // 超时 / 连接被拒 / 代理失败都归为「连不上」
        if (!cancelled) setStatus('offline')
      }
    }

    void check()
    const timer = setInterval(() => void check(), POLL_MS)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  return status
}
