/**
 * 极简 hash 路由：只负责「当前看的是哪个会话」。
 *
 * 为什么不用 react-router
 * -----------------------
 * 全站只有一个动态段（`#/s/<sessionId>`），没有嵌套路由、没有 loader、
 * 没有服务端渲染。引一个 12KB 的路由库只为解析一个字符串不划算，
 * 而且它的 history 抽象会和「SSE 流 + 会话恢复」这套状态机打架。
 *
 * 为什么是 hash 而不是 History API
 * --------------------------------
 * History API 需要服务端把所有路径回退到 index.html。本项目前端既可能由
 * Vite 托管、也可能由 FastAPI 的 StaticFiles 托管，后者对未知路径返回 404 ——
 * 用 hash 就不需要动服务端，链接在哪都能打开。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

const PREFIX = '#/s/'

/** 从 hash 里解析 sessionId。不是会话链接时返回 null。 */
export function parseSessionHash(hash: string): string | null {
  if (!hash.startsWith(PREFIX)) return null
  const id = hash.slice(PREFIX.length).trim()
  return id.length > 0 ? id : null
}

export type HashNavigate = (sessionId: string | null) => void

/**
 * 返回 ``[当前路由里的 sessionId, 导航函数]``。
 *
 * ``navigate(null)`` 会把地址退回 ``#/``（首页）。
 */
export function useHashRoute(): [string | null, HashNavigate] {
  const [routeId, setRouteId] = useState<string | null>(() =>
    parseSessionHash(window.location.hash),
  )

  // 标记「这次 hashchange 是我自己触发的」。
  // 不标记的话，navigate() 改 hash → 触发 hashchange → setRouteId →
  // 上层 effect 又调 restore，形成回环。
  const selfNavRef = useRef(false)

  useEffect(() => {
    const onHashChange = () => {
      if (selfNavRef.current) {
        selfNavRef.current = false
        return
      }
      // 用户按了前进/后退，或者手改了地址栏
      setRouteId(parseSessionHash(window.location.hash))
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const navigate = useCallback<HashNavigate>((sessionId) => {
    const target = sessionId === null ? '#/' : `${PREFIX}${sessionId}`
    // 相同地址不重复写：否则会白触发一次 hashchange，
    // 还会把 selfNavRef 标记留到下一次真实的后退操作上
    if (window.location.hash === target) {
      setRouteId(sessionId)
      return
    }
    selfNavRef.current = true
    window.location.hash = target
    setRouteId(sessionId)
  }, [])

  return [routeId, navigate]
}
