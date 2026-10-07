import { useState } from 'react'

/** 示例任务：覆盖「信息完整」「跨城」「约束冲突」三种典型输入。 */
const EXAMPLES = [
  '周末去成都玩两天，带小孩，人均预算 1000',
  '这周末去杭州玩两天，两个人',
  '预算 500 元去日本自由行 7 天，含机票',
]

interface Props {
  onSubmit: (task: string) => void
  disabled: boolean
}

export function TaskInput({ onSubmit, disabled }: Props) {
  const [value, setValue] = useState('')

  const submit = (task: string) => {
    const trimmed = task.trim()
    if (trimmed.length < 2 || disabled) return
    onSubmit(trimmed)
  }

  return (
    <>
      <form
        className={`task-input${value.length > 0 ? ' typing' : ''}`}
        onSubmit={(event) => {
          event.preventDefault()
          submit(value)
        }}
      >
        {/* 轮播**盖在** input 上，不是并排 ——
            两者都 flex:1 的话会各分一半宽度，占位文字被截断。
            有输入内容时轮播让位（.typing 类控制）。 */}
        <div className="field">
          <div className="carousel" aria-hidden="true">
            {EXAMPLES.map((example) => (
              <span key={example}>{example}</span>
            ))}
          </div>
          <input
            type="text"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            disabled={disabled}
            maxLength={500}
            aria-label="任务描述"
          />
        </div>
        <button type="submit" disabled={disabled || value.trim().length < 2}>
          开始规划
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path
              d="M2 6h8M7 3l3 3-3 3"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </form>

      <div className="chips">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            disabled={disabled}
            onClick={() => {
              // 先填进输入框再提交：用户能看到「它替我写了什么」，
              // 而不是莫名其妙就跑起来了
              setValue(example)
              submit(example)
            }}
          >
            {example}
          </button>
        ))}
      </div>
    </>
  )
}
