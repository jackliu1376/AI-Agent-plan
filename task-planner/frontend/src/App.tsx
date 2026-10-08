import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ClarificationCard } from './components/ClarificationCard'
import { Composer } from './components/Composer'
import { ConfirmDialog } from './components/ConfirmDialog'
import { EvidenceList, StageLine, buildProbes } from './components/EvidenceList'
import { HeroCollage } from './components/HeroCollage'
import { PlanView } from './components/PlanView'
import { Rail } from './components/Rail'
import { ShareButton } from './components/ShareButton'
import { fmtWhen } from './lib/formatTime'
import { TaskInput } from './components/TaskInput'
import { useBackendHealth } from './hooks/useBackendHealth'
import { NARROW_QUERY, useMediaQuery } from './hooks/useMediaQuery'
import { usePalette } from './hooks/usePalette'
import { usePlanningSession } from './hooks/usePlanningSession'
import type { HistoryEntry } from './hooks/usePlanningSession'

const RAIL_KEY = 'cairn.rail.v1'

/** 光区退场时长。必须和 styles.css 里 `.exiting` 的过渡时长对齐。 */
const HERO_EXIT_MS = 420
/** 「打开已有记录」用的短退场。计划是现成的，不该让用户等动画。 */
const HERO_EXIT_FAST_MS = 170

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

/**
 * 首次生成时的骨架屏。
 *
 * 只在「正在跑且还没有任何计划」时出现 —— 那时页面本来是空的，
 * 骨架比一句「生成中」更能说明「马上会有东西」。
 *
 * 修订时**不显示**：旧计划还在，盖掉它用户就没法对着改了。
 */
function PlanSkeleton() {
  return (
    <div className="plan-skeleton" aria-hidden="true">
      <div className="ps-bar" style={{ height: 15, width: '68%', marginBottom: 10 }} />
      <div className="ps-bar" style={{ height: 11, width: '42%', marginBottom: 30 }} />
      <div className="ps-bar" style={{ height: 11, width: '22%', marginBottom: 14 }} />
      <div className="ps-bar" style={{ height: 11, width: '92%', marginBottom: 9 }} />
      <div className="ps-bar" style={{ height: 11, width: '74%', marginBottom: 9 }} />
      <div className="ps-bar" style={{ height: 11, width: '85%', marginBottom: 30 }} />
      <div className="ps-bar" style={{ height: 11, width: '26%', marginBottom: 14 }} />
      <div className="ps-bar" style={{ height: 11, width: '88%', marginBottom: 9 }} />
      <div className="ps-bar" style={{ height: 11, width: '66%' }} />
    </div>
  )
}

export default function App() {
  const {
    state,
    start,
    answer,
    cancel,
    restore,
    revise,
    selectVersion,
    remove,
    dismissError,
    refreshHistory,
    reset,
    notify,
  } = usePlanningSession()
  const { phase, events, clarification, result, error, history, sessionId, task } = state
  const { revisions, activeSeq, pendingFeedback, leaving } = state
  const health = useBackendHealth()
  const healthStatus = health.status
  const [palette, setPalette] = usePalette()

  // 折叠状态持久化 —— 用户折叠了侧栏，刷新后不该弹回来
  const narrow = useMediaQuery(NARROW_QUERY)
  // 视差只在有精确指针（鼠标）的设备上接。触屏没有 hover，
  // 接了也只会让卡片停在某个歪着的角度上。
  const finePointer = useMediaQuery('(hover: hover) and (pointer: fine)')
  const [collapsed, setCollapsed] = useState(loadCollapsed)
  // 窄屏没有展开的余地（CSS 会把列宽锁死在 60px），所以这里强制折叠。
  // **类名和列宽必须来自同一个真相** —— 否则会出现「60px 宽但文字还在」的挤压。
  const railCollapsed = collapsed || narrow
  const toggleRail = useCallback(() => {
    setCollapsed((prev) => {
      saveCollapsed(!prev)
      return !prev
    })
  }, [])

  /**
   * 光区退场：**分两拍**。
   *
   * 第一拍（0–420ms）原地淡出，布局一动不动；
   * 第二拍才把它从 DOM 里摘掉、换上运行视图。
   *
   * 为什么必须分两拍：只淡出不动布局的话，光区一摘掉、下面的内容会「跳」上来
   * 好几百像素 —— 比硬切还难看；淡出的同时收高度的话，内容会在半透明状态下
   * 往上抽，很脏。分两拍之后，布局跳动发生在「什么都看不见」的那一瞬间。
   */
  const [heroGone, setHeroGone] = useState(false)
  useEffect(() => {
    if (leaving === 'none') {
      setHeroGone(false)
      return
    }
    const timer = setTimeout(
      () => setHeroGone(true),
      leaving === 'fast' ? HERO_EXIT_FAST_MS : HERO_EXIT_MS,
    )
    return () => clearTimeout(timer)
  }, [leaving])

  // 待删除的记录。存整条而不是 id —— 弹窗要把任务原文摆出来给用户核对，
  // 而列表刷新后按 id 可能已经查不到了。
  const [pendingDelete, setPendingDelete] = useState<HistoryEntry | null>(null)
  const requestDelete = useCallback(
    (targetId: string) => {
      const entry = history.find((item) => item.sessionId === targetId)
      if (entry !== undefined) setPendingDelete(entry)
    },
    [history],
  )

  // 「最近」列表以服务端为准（localStorage 只是首屏缓存）。
  // 两个时点必须拉一次：后端刚连上（缓存可能已经过时），
  // 以及一次规划刚结束（服务端才知道最终的 ok / 状态）。
  useEffect(() => {
    if (healthStatus !== 'online') return
    if (phase !== 'idle' && phase !== 'done' && phase !== 'failed') return
    void refreshHistory()
  }, [healthStatus, phase, refreshHistory])

  /** 顶部澄清块的「去下面回答 ↓」：滚到输入框并聚焦。 */
  const jumpToComposer = useCallback(() => {
    const box = document.getElementById('cairn-composer')
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    box?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'center' })
    document.getElementById('cairn-composer-input')?.focus()
  }, [])

  /**
   * 复制 Markdown。**必须处理失败**。
   *
   * `navigator.clipboard` 只在安全上下文（https / localhost）可用，
   * 非 https 部署时它是 `undefined`；即便存在，用户拒绝权限或浏览器
   * 限制也会 reject。原来写成 `void navigator.clipboard.writeText(...)`，
   * 失败时**完全静默** —— 用户以为复制成功了，粘出来却是旧内容，
   * 而且没有任何线索指向「复制失败」。
   */
  const copyMarkdown = useCallback(async () => {
    if (result === null) return
    try {
      if (navigator.clipboard?.writeText === undefined) {
        throw new Error('浏览器不支持剪贴板 API（通常因为页面不是 https）')
      }
      await navigator.clipboard.writeText(result.markdown)
      notify('已复制到剪贴板。')
    } catch (err) {
      notify(
        `复制失败：${err instanceof Error ? err.message : '未知原因'}。请手动选中复制。`,
      )
    }
  }, [result, notify])

  /**
   * 鼠标视差：把指针在光区内的位置映射成 -1~1，写进 --px / --py，
   * 由 .stage 的 transform 读走（见 styles.css）。
   *
   * 用 rAF 节流：mousemove 一秒能触发上百次，每次都读一遍
   * getBoundingClientRect 会强制同步布局。一帧最多算一次就够了。
   */
  const heroRef = useRef<HTMLDivElement | null>(null)
  const rafRef = useRef<number | null>(null)

  const onHeroMove = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      if (!finePointer) return
      const { clientX, clientY } = event
      if (rafRef.current !== null) return
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null
        const el = heroRef.current
        if (el === null) return
        const r = el.getBoundingClientRect()
        if (r.width === 0 || r.height === 0) return
        el.style.setProperty('--px', (((clientX - r.left) / r.width) * 2 - 1).toFixed(3))
        el.style.setProperty('--py', (((clientY - r.top) / r.height) * 2 - 1).toFixed(3))
      })
    },
    [finePointer],
  )

  const onHeroLeave = useCallback(() => {
    const el = heroRef.current
    if (el === null) return
    // 回正。不回正的话鼠标移出后卡片会一直歪着。
    el.style.setProperty('--px', '0')
    el.style.setProperty('--py', '0')
  }, [])

  useEffect(
    () => () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    },
    [],
  )

  /**
   * 拼贴的缩放系数要**同时受宽度和高度约束**。
   *
   * 宽度由 CSS 断点给（`--fit-w`）；**高度必须在这里算** ——
   * 纯 CSS 做不到：`calc(660px * var(--fit))` 要求 `--fit` 是无单位数字，
   * 而 `(100vh - 400px) / 316` 算出来是**长度**，CSS 没有「长度 ÷ 长度」
   * 这种运算（实测各种写法都返回 0px，见 styles.css 的注释）。
   *
   * 不缩的话，系统缩放到 125%（视口逻辑高度变小）时首页会顶出一条纵向滚动条 ——
   * 首页是入口，一进来就看见滚动条很掉价。
   *
   * **依赖必须包含 `heroGone`。** 光区挂载的条件是 `!heroGone`，而 `heroGone`
   * 是在另一个 effect 里复位的。从历史会话回首页时，`phase` 先变成 `idle`
   * 而 `heroGone` 还是 `true` —— 这次 effect 跑了个空（`heroRef.current` 是
   * null）；等 `heroGone` 复位、光区真正挂载时 `phase` 已经不再变化，effect
   * 不会重跑，于是 `--fit-h` 没被设置、滚动条又冒出来。
   * 表现就是「刷新后正常，走一趟历史再回来就不正常」。
   */
  useEffect(() => {
    const zone = heroRef.current
    if (zone === null) return

    const update = () => {
      const inner = zone.querySelector('.hero-inner')
      const frame = zone.querySelector('.stage-frame')
      if (inner === null || frame === null) return

      const rect = zone.getBoundingClientRect()
      const scrollY = window.scrollY

      // **量「光区之外」占掉的高度**，而不是猜一个常数 ——
      // 硬编码的固定高度总是漏掉一两处（`main-body` 的 padding-bottom
      // 就漏过一次，导致 125% 缩放下仍溢出 38px）。
      // 光区上方 + 下方的内容高度都与光区自身高度无关，所以量一次就准。
      const above = rect.top + scrollY
      const below = Math.max(0, document.documentElement.scrollHeight - (rect.bottom + scrollY))
      const outside = above + below

      const cs = getComputedStyle(zone)
      const padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)

      // 「除拼贴之外的内容高度」= 内容总高 − 拼贴高。
      // 这个差值不随 --fit 变（拼贴缩小时 inner 也等量变小），可以放心用。
      const other =
        inner.getBoundingClientRect().height - frame.getBoundingClientRect().height

      const availForCollage = window.innerHeight - outside - padY - other
      // 下限 0.3（≈95px）：再矮的视口也不该把拼贴缩成看不见的一小块 ——
      // 那种情况下让它溢出、可以滚，比给一个没意义的装饰强。
      // 实测 0.32 在 460px 视口下仍差 2px，0.30 留出了余量。
      const fit = Math.max(0.3, Math.min(1, availForCollage / 316))
      zone.style.setProperty('--fit-h', fit.toFixed(3))
    }

    // 拖窗口时 resize 会高频触发，一帧最多算一次
    let raf = 0
    const schedule = () => {
      if (raf !== 0) return
      raf = requestAnimationFrame(() => {
        raf = 0
        update()
      })
    }

    update()
    window.addEventListener('resize', schedule)
    return () => {
      window.removeEventListener('resize', schedule)
      if (raf !== 0) cancelAnimationFrame(raf)
    }
  }, [phase, heroGone])

  const busy = phase === 'running' || phase === 'awaiting'
  const started = phase !== 'idle'
  const hasPlan = result !== null && result.plan !== null

  /**
   * 页头副行。按阶段给不同信息 —— 这一行是「我在看哪个任务」的答案。
   *
   * - 跑着的时候：进行中 / 修订中 + 已查证几项（给进度感）
   * - 有计划之后：时间 · 版本数 · 步数（给定位和规模感）
   *
   * 时间从 `history` 里按 sessionId 反查 —— `state` 本身不带会话创建时间。
   * 查不到就不显示（比如刚跑完、history 还没刷新），不影响其余信息。
   */
  const echoTs =
    sessionId !== null ? (history.find((h) => h.sessionId === sessionId)?.ts ?? null) : null

  // ---- 从事件流派生的值：统一在这里算一次，向下传 ----
  //
  // 流式期间每来一个事件都会重渲染整棵树。之前 `buildProbes` 被
  // StageLine 与 EvidenceList 各算一遍，`events.filter` 再扫一遍 ——
  // 一次渲染遍历三趟，而它们要的都是同一份结果。
  // useMemo 依赖 events（每次事件确实会变，这是必要的重算），
  // 但至少从「三趟」降到「一趟」。
  const probes = useMemo(() => buildProbes(events), [events])
  const verifiedCount = useMemo(
    () => events.filter((e) => e.type === 'tool_result').length,
    [events],
  )
  const hasTurnStarted = useMemo(
    () => events.some((e) => e.type === 'turn_started'),
    [events],
  )
  const eventCount = events.length

  const echoSub = (() => {
    if (busy) {
      const label = pendingFeedback === null ? '进行中' : '修订中'
      return verifiedCount > 0 ? `${label} · 已查证 ${verifiedCount} 项` : label
    }
    if (result === null) return ''
    return [
      echoTs !== null ? fmtWhen(echoTs) : null,
      revisions.length > 0 ? `${revisions.length} 版` : null,
      result.step_count > 0 ? `${result.step_count} 步` : null,
    ]
      .filter((x): x is string => x !== null)
      .join(' · ')
  })()
  const offline = healthStatus === 'offline'
  const exiting = leaving !== 'none' && !heroGone

  // 有计划可改时输入区才出现 —— 首次运行期间不显示（那时还没有「上一版」），
  // 但结果哪怕是失败的也显示：模型说了什么，用户能看到、也能在此基础上提修改。
  const showComposer = result !== null
  const questions = result?.plan?.clarifications_needed ?? []
  const historical = activeSeq > 0 && activeSeq < revisions.length

  return (
    <div className={`shell${railCollapsed ? ' collapsed' : ''}`}>
      <Rail
        collapsed={railCollapsed}
        canToggle={!narrow}
        onToggle={toggleRail}
        onHome={reset}
        history={history}
        activeId={sessionId}
        onSelect={restore}
        onNew={reset}
        onDelete={requestDelete}
        palette={palette}
        onPaletteChange={setPalette}
        busy={busy}
        health={healthStatus}
      />

      <main className="main">
        {/* 后端连不上时说清楚 —— 否则用户会在提交后才看到报错，
            而「侧栏写着已就绪」会把排查方向带偏 */}
        {offline && (
          <div className="notice-wrap">
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
          </div>
        )}

        {/* 光区：只在「还没提交任务」时存在，提交后淡出并从 DOM 摘掉 */}
        {!heroGone && (
          <div
            ref={heroRef}
            className={`hero-zone${exiting ? ' exiting' : ''}`}
            data-palette={palette}
            data-exit={leaving === 'fast' ? 'fast' : 'normal'}
            onMouseMove={onHeroMove}
            onMouseLeave={onHeroLeave}
          >
            <div className="aurora" aria-hidden="true">
              <i />
              <i />
            </div>

            <div className="hero-inner">
              <div className="hero">
                <h1>想去哪？想做什么？</h1>
                <p>说一句就够了。它会先查证数据，再给出带依赖顺序的执行计划。</p>
              </div>

              <TaskInput onSubmit={start} disabled={busy || offline} />
              <HeroCollage />
            </div>
          </div>
        )}

        <div className={`main-body${started ? ' with-top' : ''}`}>
          {started && (
            <div className="echo">
              {/* 任务名 + 副行包成一体，这样右侧的分享按钮对齐的是**整块**，
                  而不是只对齐第一行 —— 否则按钮会悬在两行之间的高度上。 */}
              <div className="echo-body">
                <div className="echo-task">{task}</div>
                {echoSub !== '' && <div className="echo-when">{echoSub}</div>}
              </div>
              {/* 分享入口放这一行的右侧（`margin-left: auto`）——
                  它在页面右上角，和用户扫一眼找「分享」的位置一致。
                  只在有计划后出现：没东西可分享时摆个按钮是噪音。 */}
              {hasPlan && <ShareButton plan={result.plan!} model={health.model} />}
            </div>
          )}

          {started && busy && (
            <StageLine
              probes={probes}
              eventCount={eventCount}
              turned={hasTurnStarted}
              busy={busy}
              revising={pendingFeedback !== null}
            />
          )}

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
                ) : phase === 'failed' ? (
                  <button type="button" className="btn quiet" onClick={reset}>
                    重新开始
                  </button>
                ) : (
                  /* 会话本身没问题，只是一条提示（比如后端是旧版本）。
                     这里给「重新开始」是错的 —— 点一下就把手上的计划丢了。 */
                  <button type="button" className="btn quiet" onClick={dismissError}>
                    知道了
                  </button>
                )}
              </span>
            </div>
          )}

          {clarification !== null && <ClarificationCard data={clarification} onSubmit={answer} />}

          {started && <EvidenceList probes={probes} busy={busy} />}

          {/* 首次生成：还没有任何计划，用骨架占位 */}
          {started && busy && !hasPlan && <PlanSkeleton />}

          {/* 修订中把旧计划压暗 —— 保留可读，但一眼看出「这是旧的」。
              busy 时 pointer-events 也被关掉（见 .plan-dimmed）。 */}
          {hasPlan && (
            <div className={busy ? 'plan-dimmed' : undefined} aria-busy={busy || undefined}>
              <PlanView
                plan={result.plan!}
                markdown={result.markdown}
                stepCount={result.step_count}
                seq={activeSeq}
                revisionSummary={result.revision_summary}
                historical={historical}
                onJumpToComposer={showComposer ? jumpToComposer : undefined}
              />
            </div>
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

          {/* 页脚只留「复制 Markdown」和运行中的「取消」。
              「规划新任务」删掉了 —— 侧栏和底部输入区各有一个「新建任务」，
              第三个入口是冗余；而且它和「复制」并排时，
              用户点错一下就把手上的计划丢了。 */}
          {started && (busy || hasPlan) && (
            <div className="foot">
              <span className="spacer" />
              {busy && (
                <button type="button" className="btn" onClick={() => void cancel()}>
                  取消
                </button>
              )}
              {hasPlan && (
                <button
                  type="button"
                  className="btn primary"
                  onClick={() => void copyMarkdown()}
                >
                  复制 Markdown
                </button>
              )}
            </div>
          )}
        </div>

        {showComposer && (
          <Composer
            questions={questions}
            seq={activeSeq}
            revisions={revisions}
            busy={busy}
            pendingFeedback={pendingFeedback}
            model={health.model}
            onSubmit={(text) => void revise(text)}
            onSelectVersion={(target) => void selectVersion(target)}
            onNewTask={reset}
            onCancel={() => void cancel()}
          />
        )}
      </main>

      {/* 删除是硬删除（磁盘那一行也会没），所以必须二次确认。
          正在跑的会话被删会连带取消，文案得不一样。 */}
      {pendingDelete !== null && (
        <ConfirmDialog
          title="删除这条记录？"
          note={
            pendingDelete.ok === null
              ? '这次规划正在运行，删除会一并取消。删除后无法恢复。'
              : '删除后无法恢复。这条记录和它产出的计划会一起从磁盘上移除。'
          }
          quote={pendingDelete.task}
          confirmLabel="删除"
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            const targetId = pendingDelete.sessionId
            setPendingDelete(null)
            void remove(targetId)
          }}
        />
      )}
    </div>
  )
}
