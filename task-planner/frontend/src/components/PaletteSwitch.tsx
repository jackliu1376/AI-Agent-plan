/**
 * 光区配色切换。
 *
 * 放侧栏底部而不是光区里：光区里一旦有了计划就够不着了，
 * 而这是个常驻偏好。侧栏折叠成 60px 时退化成两个色块（竖排）。
 *
 * 选中态用「白底 + 发丝线」而不是高亮色 —— 这是**选择**，
 * 不是「需要注意」。用高亮色会破坏「一色一义」。
 *
 * 用 radiogroup 而不是两个独立按钮：语义上它就是在几个选项里选一个，
 * 顺带拿到方向键的键盘约定。
 */

import type { Palette } from '../hooks/usePalette'

interface Props {
  value: Palette
  onChange: (next: Palette) => void
  disabled?: boolean
}

const OPTIONS: { id: Palette; label: string }[] = [
  { id: 'cool', label: '蓝紫' },
  { id: 'brand', label: '暖色' },
]

export function PaletteSwitch({ value, onChange, disabled = false }: Props) {
  const move = (event: React.KeyboardEvent, index: number) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    const step = event.key === 'ArrowRight' ? 1 : OPTIONS.length - 1
    const next = OPTIONS[(index + step) % OPTIONS.length]
    if (next === undefined) return
    onChange(next.id)
    const group = event.currentTarget.parentElement
    group?.querySelectorAll<HTMLButtonElement>('.seg-i')[OPTIONS.indexOf(next)]?.focus()
  }

  return (
    <div className="seg" role="radiogroup" aria-label="光区配色">
      {OPTIONS.map((option, index) => (
        <button
          key={option.id}
          type="button"
          role="radio"
          aria-checked={option.id === value}
          className="seg-i"
          disabled={disabled}
          onClick={() => onChange(option.id)}
          onKeyDown={(event) => move(event, index)}
        >
          <i className={`sw sw-${option.id}`} aria-hidden="true" />
          <span>{option.label}</span>
        </button>
      ))}
    </div>
  )
}
