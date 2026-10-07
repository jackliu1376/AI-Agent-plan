/**
 * 二次确认弹窗。
 *
 * 为什么不用 `window.confirm`
 * ---------------------------
 * 原生 confirm 给不出三样东西，而这三样恰好是删除这类操作最需要的：
 *
 * 1. **把要删的东西摆出来。** 用户点的是列表里第 3 行，弹窗里必须能确认
 *    删的是哪一条，而不是靠记忆。原生 confirm 只能显示一句纯文本。
 * 2. **说清后果。** 会话是**硬删除**（磁盘那一行也会没），
 *    而 `logs/runs.jsonl` 里没有计划正文 —— 删了就真没了，这话必须说。
 * 3. **按钮措辞。** 原生 confirm 只有「确定 / 取消」，而「确定」是个
 *    不需要读的词。破坏性操作的按钮要写动词（「删除」），逼用户读一遍。
 *
 * 四个容易漏的细节
 * ----------------
 * - **默认焦点在「取消」。** 连按两次回车 = 取消 + 关闭，不会误删。
 * - **焦点陷阱。** 不做的话 Tab 会跑到后面的侧栏去，键盘用户就迷路了。
 * - **关闭后焦点还回去。** 还回触发它的那个按钮，否则焦点掉到 body。
 * - **遮罩用 mousedown 判断。** 在弹窗里按下、拖到遮罩上松开，
 *   不该被当成「点了遮罩」而关闭。
 *
 * 用 portal 挂到 body：`.rail` 是 grid 子项，留在里面得跟层级和裁剪较劲。
 */

import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

interface Props {
  title: string
  /** 后果说明。要写清楚「删了会怎样」，而不是笼统的「确定吗」 */
  note: string
  /** 被操作对象的原文（任务描述），让用户确认点的是哪一条 */
  quote: string
  confirmLabel: string
  onCancel: () => void
  onConfirm: () => void
}

export function ConfirmDialog({
  title,
  note,
  quote,
  confirmLabel,
  onCancel,
  onConfirm,
}: Props) {
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    // 焦点先落到「取消」：安全默认
    const previous = document.activeElement
    cancelRef.current?.focus()

    // 锁住背景滚动，否则弹窗开着还能把后面的页面滚走
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      document.body.style.overflow = previousOverflow
      if (previous instanceof HTMLElement && document.contains(previous)) {
        previous.focus()
      }
    }
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onCancel()
        return
      }
      if (event.key !== 'Tab') return

      // 焦点陷阱：Tab 只在弹窗内的按钮之间循环
      const buttons = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>('button:not([disabled])') ?? [],
      )
      const first = buttons.at(0)
      const last = buttons.at(-1)
      if (first === undefined || last === undefined) return

      const active = document.activeElement
      const inside = dialogRef.current?.contains(active) ?? false

      if (!inside) {
        event.preventDefault()
        first.focus()
      } else if (event.shiftKey && active === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onCancel])

  return createPortal(
    <div
      className="scrim"
      onMouseDown={(event) => {
        // 只有「按下」就落在遮罩上才算点外部 ——
        // 在弹窗里按下、拖出去松开，不该关掉
        if (event.target === event.currentTarget) onCancel()
      }}
    >
      <div
        className="dialog"
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        aria-describedby="dialog-note"
      >
        <h2 className="dialog-title" id="dialog-title">
          {title}
        </h2>
        <p className="dialog-note" id="dialog-note">
          {note}
        </p>
        {quote.length > 0 && <div className="dialog-quote">{quote}</div>}
        <div className="dialog-actions">
          <button type="button" className="btn" ref={cancelRef} onClick={onCancel}>
            取消
          </button>
          <button type="button" className="btn danger" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
