import { useState } from 'react'
import { ApiError, login } from '../api/client'

interface Props {
  /** 登录成功。调用方负责重新加载页面 —— 此时所有 hook 都已经按未登录
   *  状态初始化过（会话列表请求被 401 挡掉），就地切换会留下一堆空状态。 */
  onSuccess: () => void
}

/**
 * 访问令牌输入页。
 *
 * 只在服务端配置了 `WEB_ACCESS_TOKEN` 时出现。本地开发留空该变量，
 * 这个组件永远不会渲染。
 */
export function LoginGate({ onSuccess }: Props) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    const value = token.trim()
    if (busy || value.length === 0) return

    setBusy(true)
    setError('')
    try {
      await login(value)
      onSuccess()
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setError('尝试次数过多，请一分钟后再试。')
      } else if (err instanceof ApiError && err.status === 401) {
        setError('令牌不正确。')
      } else {
        setError(err instanceof Error ? err.message : '登录失败，请重试。')
      }
      setBusy(false)
    }
  }

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <p className="gate-brand">Cairn</p>
        <h1 className="gate-title">需要访问令牌</h1>
        <p className="gate-desc">这个实例开启了访问保护，输入令牌后即可使用。</p>

        <label className="gate-field">
          <span className="gate-label">访问令牌</span>
          <input
            type="password"
            className="gate-input"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="粘贴部署时设置的令牌"
            autoFocus
            autoComplete="current-password"
            spellCheck={false}
            disabled={busy}
          />
        </label>

        {error.length > 0 && (
          <p className="gate-error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="gate-submit" disabled={busy || token.trim().length === 0}>
          {busy ? '验证中…' : '进入'}
        </button>

        <p className="gate-hint">令牌由部署者设置，在服务器的 .env 里，变量名 WEB_ACCESS_TOKEN。</p>
      </form>
    </div>
  )
}
