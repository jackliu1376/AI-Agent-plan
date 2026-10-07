/**
 * 光区配色（蓝紫 / 暖色）。
 *
 * 用户两套都喜欢，所以做成可切换的偏好，而不是二选一。
 *
 * 为什么存在 localStorage 而不是服务端：这是**纯外观偏好**，
 * 跟会话、计划都没关系。放服务端要为它开接口、写库、做同步，
 * 而收益是「换台机器颜色跟着走」—— 没人会需要这个。
 * （侧栏折叠状态也是同样的处理，见 App.tsx。）
 *
 * 作用域只有光区。外面那些彩色是**语义色**（绿=完成、玫瑰=要注意、
 * 琥珀=进行中），不能跟着主题变，否则「一色一义」这条约定就废了。
 */

import { useCallback, useEffect, useState } from 'react'

export type Palette = 'cool' | 'brand'

const KEY = 'cairn.palette.v1'
const DEFAULT: Palette = 'cool'

function load(): Palette {
  try {
    return localStorage.getItem(KEY) === 'brand' ? 'brand' : DEFAULT
  } catch {
    // 隐私模式 / 配额满：读不到就用默认，不影响使用
    return DEFAULT
  }
}

export function usePalette(): [Palette, (next: Palette) => void] {
  const [palette, setPalette] = useState<Palette>(load)

  useEffect(() => {
    try {
      localStorage.setItem(KEY, palette)
    } catch {
      /* 记不住就记不住 */
    }
  }, [palette])

  const choose = useCallback((next: Palette) => setPalette(next), [])
  return [palette, choose]
}
