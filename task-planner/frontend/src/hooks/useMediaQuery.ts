/**
 * 媒体查询订阅。
 *
 * 为什么需要它：侧栏在窄屏下会被 CSS 强制压成 60px 宽，
 * 但「把文字藏起来」的那些规则挂在 `.shell.collapsed` 上 ——
 * 只改网格列宽而不加类，结果就是侧栏变成 60px 却还渲染着全部文字，
 * 挤成一列竖排。
 *
 * 所以窄屏判断要进 JS，让**类名和列宽来自同一个真相**，
 * 而不是 CSS 一套、DOM 一套各说各话。
 */

import { useEffect, useState } from 'react'

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)

  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange()
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])

  return matches
}

/** 窄屏断点，和 styles.css 里的 `@media (max-width: 760px)` 对齐。 */
export const NARROW_QUERY = '(max-width: 760px)'
