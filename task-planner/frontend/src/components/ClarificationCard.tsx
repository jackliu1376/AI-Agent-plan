/**
 * 澄清卡片。
 *
 * 对应后端的「暂停 → 提问 → 恢复」机制：服务端协程挂在一个 Future 上，
 * 这里提交答案后由 POST /answers 唤醒它继续跑。
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
    <section className="panel clarify">
      <div className="panel-head">
        <h2>需要你补充信息</h2>
      </div>
      <div className="panel-body">
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
          className="composer-row"
          onSubmit={(e) => {
            e.preventDefault()
            submit(value)
          }}
        >
          <input
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="直接回答，比如「成都，10 月 3-4 日，2 大 1 小」"
            maxLength={1000}
            autoFocus
            aria-label="澄清答案"
          />
          <button type="submit" className="btn btn-primary" disabled={value.trim().length === 0}>
            提交
          </button>
        </form>
      </div>
    </section>
  )
}
