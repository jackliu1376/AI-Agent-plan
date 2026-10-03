import { useState } from 'react'

const EXAMPLES = [
  '我想周末去成都玩两天，带小孩，人均预算 1000',
  '帮我规划这周末去杭州玩两天，两个人',
  '预算 500 元去日本自由行 7 天，含机票',
  '帮我把毕业论文写完',
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
    <div className="composer">
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
          placeholder="说说你想做什么，比如「周末去成都玩两天，带小孩，人均预算 1000」"
          disabled={disabled}
          maxLength={500}
          aria-label="任务描述"
        />
        <button
          type="submit"
          className="btn btn-primary"
          disabled={disabled || value.trim().length < 2}
        >
          开始规划
        </button>
      </form>

      <div className="examples">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            disabled={disabled}
            onClick={() => {
              setValue(example)
              submit(example)
            }}
          >
            {example.length > 22 ? `${example.slice(0, 22)}…` : example}
          </button>
        ))}
      </div>
    </div>
  )
}
