/**
 * 澄清卡片。
 *
 * 对应后端的「暂停 → 提问 → 恢复」机制：服务端协程挂在一个 Future 上，
 * 这里提交答案后由 POST /answers 唤醒它继续跑。
 *
 * 视觉上刻意与「进度」区分开：玫瑰底色 + 明确的问题，因为这是**唯一**
 * 需要用户动手的一步，不该混在信息流里。
 */

import { useState } from 'react'
import type { ClarificationData } from '../api/types'

interface Props {
  data: ClarificationData
  onSubmit: (answer: string) => void
}

export function ClarificationCard({ data, onSubmit }: Props) {
  const [value, setValue] = useState('')

  const submit = (answer: string) => {
    const trimmed = answer.trim()
    if (trimmed.length === 0) return
    onSubmit(trimmed)
  }

  return (
    <div className="note ask" role="group" aria-label="需要你补充信息">
      <svg width="13" height="13" viewBox="0 0 12 12" fill="none" aria-hidden="true">
        <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1.4" />
        <path d="M6 4.4v.1M6 5.9v2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="clarify-question">{data.question}</div>

        {data.reason.length > 0 && <p className="clarify-reason">原因：{data.reason}</p>}

        {data.options.length > 0 && (
          <div className="clarify-options">
            {data.options.map((option) => (
              <button key={option} type="button" onClick={() => submit(option)}>
                {option}
              </button>
            ))}
          </div>
        )}

        <form
          className="clarify-form"
          onSubmit={(event) => {
            event.preventDefault()
            submit(value)
          }}
        >
          <input
            type="text"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="直接回答，比如「成都，10 月 3-4 日，2 大 1 小」"
            maxLength={1000}
            autoFocus
            aria-label="澄清答案"
          />
          <button type="submit" className="btn primary" disabled={value.trim().length === 0}>
            提交
          </button>
        </form>
      </div>
    </div>
  )
}
