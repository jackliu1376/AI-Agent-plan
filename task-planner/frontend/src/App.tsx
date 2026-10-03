import { ClarificationCard } from './components/ClarificationCard'
import { PlanView } from './components/PlanView'
import { ProgressFeed } from './components/ProgressFeed'
import { TaskInput } from './components/TaskInput'
import { usePlanningSession } from './hooks/usePlanningSession'

export default function App() {
  const { state, start, answer, reset } = usePlanningSession()
  const { phase, events, clarification, result, error } = state

  const busy = phase === 'running' || phase === 'awaiting'
  const started = phase !== 'idle'

  return (
    <div className="app">
      <header className="header">
        <h1>任务规划助手</h1>
        <p>说一句你想做什么，它调用工具取证后，产出一份带依赖关系的可执行计划。</p>
      </header>

      <TaskInput onSubmit={start} disabled={busy} />

      {error !== null && (
        <div className="banner error" role="alert">
          <span>{error}</span>
          <button type="button" className="btn btn-ghost" onClick={reset}>
            重新开始
          </button>
        </div>
      )}

      {clarification !== null && <ClarificationCard data={clarification} onSubmit={answer} />}

      <ProgressFeed events={events} busy={busy} />

      {result !== null && result.plan !== null && (
        <PlanView plan={result.plan} markdown={result.markdown} stepCount={result.step_count} />
      )}

      {/* 模型没能产出合规 JSON 时，至少把它的自然语言回复展示出来（常见于拒绝或指出约束冲突） */}
      {result !== null && result.plan === null && result.markdown.trim().length > 0 && (
        <section className="panel">
          <div className="panel-head">
            <h2>回复</h2>
          </div>
          <pre className="raw">{result.markdown}</pre>
        </section>
      )}

      {started && !busy && (
        <div className="actions">
          <button type="button" className="btn btn-ghost" onClick={reset}>
            规划新任务
          </button>
        </div>
      )}
    </div>
  )
}
