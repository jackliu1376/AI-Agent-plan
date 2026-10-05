import { useCallback, useEffect, useState } from 'react'
import { ClarificationCard } from './components/ClarificationCard'
import { EvidenceList, StageLine } from './components/EvidenceList'
import { PlanView } from './components/PlanView'
import { Rail } from './components/Rail'
import { TaskInput } from './components/TaskInput'
import { useBackendHealth } from './hooks/useBackendHealth'
import { usePlanningSession } from './hooks/usePlanningSession'

const RAIL_KEY = 'cairn.rail.v1'

function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) === 'collapsed'
  } catch {
    return false
  }
}

function saveCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(RAIL_KEY, collapsed ? 'collapsed' : 'expanded')
  } catch {
    /* 记不住就记不住，不影响使用 */
  }
}

export default function App() {
  const { state, start, answer, cancel, restore, refreshHistory, reset } = usePlanningSession()
  const { phase, events, clarification, result, error, history, sessionId, task } = state
  const health = useBackendHealth()

  // 折叠状态持久化 —— 用户折叠了侧栏，刷新后不该弹回来
  const [collapsed, setCollapsed] = useState(loadCollapsed)
  const toggleRail = useCallback(() => {
    setCollapsed((prev) => {
      saveCollapsed(!prev)
      return !prev
    })
  }, [])

  // 「最近」列表以服务端为准（localStorage 只是首屏缓存）。
  // 两个时点必须拉一次：后端刚连上（缓存可能已经过时），
  // 以及一次规划刚结束（服务端才知道最终的 ok / 状态）。
  useEffect(() => {
    if (health !== 'online') return
    if (phase !== 'idle' && phase !== 'done' && phase !== 'failed') return
    void refreshHistory()
  }, [health, phase, refreshHistory])

  const busy = phase === 'running' || phase === 'awaiting'
  const started = phase !== 'idle'
  const hasPlan = result !== null && result.plan !== null
  const offline = health === 'offline'

  return (
    <div className={`shell${collapsed ? ' collapsed' : ''}`}>
      <Rail
        collapsed={collapsed}
        onToggle={toggleRail}
        history={history}
        activeId={sessionId}
        onSelect={restore}
        onNew={reset}
        busy={busy}
        health={health}
      />

      <main className={`main${started ? '' : ' narrow'}`}>
        {/* 后端连不上时说清楚 —— 否则用户会在提交后才看到报错，
            而「侧栏写着已就绪」会把排查方向带偏 */}
        {offline && (
          <div className="note danger" role="alert">
            <svg width="13" height="13" viewBox="0 0 12 12" fill="none" aria-hidden="true">
              <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1.4" />
              <path d="M6 3.4v3M6 8.2v.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
            <span>
              连不上后端服务。请在另一个终端运行
              <code style={{ fontFamily: 'var(--mono)', fontSize: 12 }}> uv run task-planner-web</code>
              ，然后刷新本页。
            </span>
          </div>
        )}

        {!started && (
          <div className="hero">
            <h1>想去哪？想做什么？</h1>
            <p>说一句就够了。它会先查证数据，再给出带依赖顺序的执行计划。</p>
          </div>
        )}

        {!started && <TaskInput onSubmit={start} disabled={busy || offline} />}

        {started && (
          <div className="echo">
            <div className="echo-task">{task}</div>
            {busy && (
              <div className="echo-meta">
                <span className="tag">
                  <span className="spinner" aria-hidden="true" />
                  进行中
                </span>
              </div>
            )}
          </div>
        )}

        {started && busy && <StageLine events={events} busy={busy} />}

        {error !== null && (
          <div className="note danger" role="alert">
            <svg width="13" height="13" viewBox="0 0 12 12" fill="none" aria-hidden="true">
              <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1.4" />
              <path d="M6 3.4v3M6 8.2v.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
            <span>{error}</span>
            <span className="note-actions">
              {busy ? (
                <button type="button" className="btn quiet" onClick={() => void cancel()}>
                  取消
                </button>
              ) : (
                <button type="button" className="btn quiet" onClick={reset}>
                  重新开始
                </button>
              )}
            </span>
          </div>
        )}

        {clarification !== null && <ClarificationCard data={clarification} onSubmit={answer} />}

        {started && <EvidenceList events={events} busy={busy} />}

        {hasPlan && (
          <PlanView
            plan={result.plan!}
            markdown={result.markdown}
            stepCount={result.step_count}
          />
        )}

        {/* 模型没能产出合规 JSON 时，至少把自然语言回复展示出来
            （常见于拒绝执行，或指出约束冲突） */}
        {result !== null && result.plan === null && result.markdown.trim().length > 0 && (
          <>
            <div className="note warn">
              <svg width="13" height="13" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                <path
                  d="M6 1.6l4.6 8H1.4l4.6-8z"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinejoin="round"
                />
                <path d="M6 5v2M6 8.4v.1" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
              </svg>
              <span>模型没有产出结构化的计划，下面是它的原始回复。</span>
            </div>
            <pre className="raw">{result.markdown}</pre>
          </>
        )}

        {started && (
          <div className="foot">
            {hasPlan && (
              <button
                type="button"
                className="btn primary"
                onClick={() => void navigator.clipboard.writeText(result.markdown)}
              >
                复制 Markdown
              </button>
            )}
            <span className="spacer" />
            {busy && (
              <button type="button" className="btn" onClick={() => void cancel()}>
                取消
              </button>
            )}
            {!busy && (
              <button type="button" className="btn quiet" onClick={reset}>
                规划新任务
              </button>
            )}
          </div>
        )}
      </main>
    </div>
  )
}
