/**
 * 底部输入区 —— 回答澄清、提修改、切版本、开新任务，都在这里。
 *
 * 为什么底部常驻，而不是内嵌在澄清块旁边
 * --------------------------------------
 * 用户想改的时候，人往往在文档中间或底部。一份 17 步的计划，让他滚回顶部
 * 去找输入框是反人性的。底部常驻，任何位置都够得着。
 *
 * 代价是「问题在顶部、输入框在底部」，中间隔着十几个步骤 ——
 * 所以澄清问题要**下沉成芯片**带到输入框上方（见 `.composer-chips`）。
 * 这是有用的冗余，不是重复：顶部那块是「你需要决定什么」的告知，
 * 底部芯片是「现在就能答」的入口。
 *
 * 为什么只有一个输入框
 * --------------------
 * 回答澄清和提出修改本质是同一个动作 ——「这是新信息，更新计划」。
 * 区别只在模型拿到之后干什么：补一个字段，还是改结构。做两个框是假的区分。
 *
 * 工具条上为什么只有四样东西
 * --------------------------
 * `新建任务` / `版本` / `模型名` / `发送`。参考图里的分支、权限、麦克风在
 * 这个产品里没有对应物，硬搬就是**假控件** —— 比不放更糟。
 * 模型名做成灰底只读 chip（信息）而不是白底按钮（控件），也是同一个道理：
 * 前端目前没有切换模型的入口，画个箭头就是骗人。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { RevisionSummary } from '../api/types'

/** 问题太长就截短做芯片。截的是「（）」里的举例和问号，留下主干。 */
function chipLabel(question: string): string {
  const stem = question.split(/[（(？?：:]/)[0]?.trim() ?? ''
  const short = stem.length > 0 ? stem : question
  return short.length > 12 ? `${short.slice(0, 12)}…` : short
}

/** 「刚刚」「12 分钟前」「09:03」。版本菜单里用，不需要精确到秒。 */
function relativeTime(seconds: number): string {
  const delta = Date.now() / 1000 - seconds
  if (delta < 60) return '刚刚'
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`
  const date = new Date(seconds * 1000)
  const today = new Date()
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate()
  const hhmm = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  return sameDay ? hhmm : `${date.getMonth() + 1}-${date.getDate()} ${hhmm}`
}

/** 版本菜单：点芯片后在**上方**展开。输入框在页面底部，往下弹会被视口裁掉。 */
function VersionMenu({
  seq,
  revisions,
  onSelect,
  disabled,
}: {
  seq: number
  revisions: RevisionSummary[]
  onSelect: (seq: number) => void
  disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const holderRef = useRef<HTMLSpanElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)

  // 打开后把焦点移到当前选中项（没有就移到第一项）。
  //
  // 不这么做的话焦点仍留在触发按钮上，而方向键处理挂在菜单容器上 ——
  // 键盘用户**根本进不去菜单**：按 ↑↓ 毫无反应，Tab 则会直接跳过整块。
  useEffect(() => {
    if (!open) return
    const items = Array.from(
      holderRef.current?.querySelectorAll<HTMLButtonElement>('.vmenu-item') ?? [],
    )
    const current =
      items.find((el) => el.getAttribute('aria-checked') === 'true') ?? items[0]
    current?.focus()
  }, [open])

  // 点外面关、Esc 关。用 mousedown 而不是 click：
  // 在菜单里按下、拖到外面松开，不该被当成「点了外部」。
  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!holderRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false)
        // 焦点还给触发按钮。不还的话它会掉到 <body> 上，
        // 键盘用户按 Tab 就得从头再走一遍整个页面。
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const onKeyDown = (event: React.KeyboardEvent) => {
    const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End']
    if (!keys.includes(event.key)) return
    event.preventDefault()
    const items = Array.from(
      holderRef.current?.querySelectorAll<HTMLButtonElement>('.vmenu-item') ?? [],
    )
    if (items.length === 0) return

    if (event.key === 'Home') {
      items[0]?.focus()
      return
    }
    if (event.key === 'End') {
      items[items.length - 1]?.focus()
      return
    }
    // -1 表示焦点还不在任何一项上（例如刚用鼠标点开）——
    // 此时 ↓ 应落到第一项、↑ 落到最后一项。
    const current = items.indexOf(document.activeElement as HTMLButtonElement)
    const step = event.key === 'ArrowDown' ? 1 : -1
    const next = current === -1 ? (step === 1 ? 0 : items.length - 1) : current + step
    items[(next + items.length) % items.length]?.focus()
  }

  // 最新一版排最前 —— 用户多数时候想看的是「和上一版的差别」
  const ordered = [...revisions].sort((a, b) => b.seq - a.seq)

  return (
    <span className="vmenu-holder" ref={holderRef} onKeyDown={onKeyDown}>
      {open && (
        <div className="vmenu" role="menu" aria-label="历史版本">
          <div className="vmenu-head">历史版本</div>
          {ordered.map((item) => (
            <button
              key={item.seq}
              type="button"
              role="menuitemradio"
              aria-checked={item.seq === seq}
              className="vmenu-item"
              onClick={() => {
                setOpen(false)
                onSelect(item.seq)
              }}
            >
              <span className="vmenu-ic" aria-hidden="true">
                <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
                  <circle cx="8" cy="8" r="5.6" stroke="currentColor" strokeWidth="1.25" />
                  <path
                    d="M8 5.2V8l2 1.2"
                    stroke="currentColor"
                    strokeWidth="1.25"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <span className="vmenu-tx">
                <span className="vmenu-t1">
                  第 {item.seq} 版
                  {!item.ok && <em className="vmenu-bad">未产出计划</em>}
                </span>
                <span className="vmenu-t2">
                  {item.feedback.length > 0 ? item.feedback : '原始需求'} ·{' '}
                  {relativeTime(item.created_at)}
                </span>
              </span>
              <span className="vmenu-ck" aria-hidden="true">
                <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
                  <path
                    d="M3 7.4l2.6 2.6L11 4.4"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
            </button>
          ))}
        </div>
      )}
      <button
        type="button"
        ref={triggerRef}
        className={`cbar-btn${open ? ' on' : ''}`}
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        第 {seq} 版
      </button>
    </span>
  )
}

interface Props {
  /** 计划里待用户确认的问题。有就做成芯片。 */
  questions: string[]
  /** 当前看的第几版；0 表示还没有版本 */
  seq: number
  revisions: RevisionSummary[]
  busy: boolean
  /** 正在修订时回显的用户原话 */
  pendingFeedback: string | null
  model: string
  onSubmit: (text: string) => void
  onSelectVersion: (seq: number) => void
  onNewTask: () => void
  onCancel: () => void
}

export function Composer({
  questions,
  seq,
  revisions,
  busy,
  pendingFeedback,
  model,
  onSubmit,
  onSelectVersion,
  onNewTask,
  onCancel,
}: Props) {
  const [text, setText] = useState('')
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  // 高度跟着内容长，最多 5 行左右（再长就内部滚动）
  const resize = useCallback(() => {
    const el = inputRef.current
    if (el === null) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 96)}px`
  }, [])

  useEffect(() => {
    // 修订结束后把输入框清空并恢复可编辑 —— 用户可以接着提下一条
    if (!busy) {
      setText('')
      resize()
    }
  }, [busy, resize])

  const submit = () => {
    const value = text.trim()
    if (value.length === 0 || busy) return
    onSubmit(value)
  }

  const canSend = text.trim().length > 0 && !busy

  return (
    <div className="composer" id="cairn-composer">
      <div className="composer-inner">
        {questions.length > 0 && !busy && (
          <div className="composer-chips">
            {questions.map((question) => (
              <button
                key={question}
                type="button"
                className="chip"
                onClick={() => {
                  setText(`${chipLabel(question)}：`)
                  inputRef.current?.focus()
                  resize()
                }}
              >
                {chipLabel(question)}
              </button>
            ))}
          </div>
        )}

        <div className="composer-box">
          <textarea
            id="cairn-composer-input"
            ref={inputRef}
            className="composer-input"
            rows={1}
            disabled={busy}
            value={busy ? (pendingFeedback ?? '') : text}
            placeholder={
              questions.length > 0
                ? '回答上面的问题，或直接说要改哪里…'
                : '想改哪里？比如「第二天太赶了」'
            }
            onChange={(event) => {
              setText(event.target.value)
              resize()
            }}
            onKeyDown={(event) => {
              // Enter 发送，Shift+Enter 换行 —— 和所有聊天框一致
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                submit()
              }
            }}
          />

          <div className="composer-bar">
            <button
              type="button"
              className="cbar-btn"
              onClick={onNewTask}
              disabled={busy}
            >
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
              新建任务
            </button>

            {seq > 0 && revisions.length > 1 && (
              <VersionMenu
                seq={seq}
                revisions={revisions}
                onSelect={onSelectVersion}
                disabled={busy}
              />
            )}

            <span className="cbar-grow" />

            {model.length > 0 && <span className="cbar-chip">{model}</span>}

            {busy ? (
              <button
                type="button"
                className="cbar-stop"
                onClick={onCancel}
                aria-label="停止"
                title="停止"
              >
                <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true">
                  <rect x="2" y="2" width="8" height="8" rx="1.6" fill="currentColor" />
                </svg>
              </button>
            ) : (
              <button
                type="button"
                className="cbar-send"
                onClick={submit}
                disabled={!canSend}
                aria-label="发送"
                title="发送（Enter）"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path
                    d="M12 19.5V5M12 5l-6 6M12 5l6 6"
                    stroke="currentColor"
                    strokeWidth="2.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            )}
          </div>
        </div>

        {busy && (
          <div className="composer-hint">
            {pendingFeedback === null ? '正在规划…' : '正在按你的反馈修订…'}
          </div>
        )}
      </div>
    </div>
  )
}
