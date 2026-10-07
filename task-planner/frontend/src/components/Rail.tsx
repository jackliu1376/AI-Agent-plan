/**
 * 左侧栏：品牌 / 折叠 / 新建任务 / 最近任务 / 数据源状态。
 *
 * 折叠时只留图形 —— 历史条目收成「首字方块」而不是圆点：
 * 只留圆点的话，用户认不出哪条是哪个任务。
 *
 * 历史来自服务端（`logs/sessions.db`），不是 localStorage ——
 * 所以列表里的每一条都确实还在，点开就有东西。
 *
 * 悬浮时才出现的删除按钮：`<button>` 不能嵌套，所以行外面包了一层
 * `.rail-row`，删除按钮绝对定位盖在行的右侧。行内**永久预留**了
 * 30px 右内边距，所以按钮出现时文字不会跳。
 */

import type { HistoryEntry } from '../hooks/usePlanningSession'
import type { BackendStatus } from '../hooks/useBackendHealth'
import type { Palette } from '../hooks/usePalette'
import type { SessionStatus } from '../api/types'
import { PaletteSwitch } from './PaletteSwitch'

/** Cairn 的品牌标记：三块叠石。用 inline SVG 而不是图片，颜色随主题。 */
function BrandMark({ size = 24 }: { size?: number }) {
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role="img"
      aria-label="Cairn"
    >
      <rect width="24" height="24" rx="7.5" fill="var(--ink)" />
      <ellipse cx="12" cy="16.8" rx="5.6" ry="2.7" fill="#fff" />
      <ellipse cx="12" cy="12.2" rx="4.2" ry="2.3" fill="#fff" />
      <ellipse cx="12" cy="8.3" rx="2.7" ry="1.8" fill="#fff" />
    </svg>
  )
}

/** 侧栏折叠图标：圆角方框 + 左侧分隔线。 */
function PanelIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect
        x="1.5"
        y="2.8"
        width="13"
        height="10.4"
        rx="3"
        stroke="currentColor"
        strokeWidth="1.35"
      />
      <path d="M6.2 2.8v10.4" stroke="currentColor" strokeWidth="1.35" />
    </svg>
  )
}

/** 删除图标：13px 描边垃圾桶，与侧栏其它图标同一套线宽。 */
function TrashIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <path d="M2.7 3.9h8.6" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
      <path
        d="M5.6 3.9V2.6h2.8v1.3"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
      <path
        d="M4.1 3.9l.5 7.3h4.8l.5-7.3"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

interface Props {
  collapsed: boolean
  /** 窄屏下侧栏被锁死在折叠态，这时不该给一个点了没反应的按钮 */
  canToggle: boolean
  onToggle: () => void
  /** 点品牌区回到首页（空状态） */
  onHome: () => void
  history: HistoryEntry[]
  activeId: string | null
  onSelect: (sessionId: string) => void
  onNew: () => void
  /** 请求删除一条记录（真正的删除由上层弹确认框后再执行） */
  onDelete: (sessionId: string) => void
  palette: Palette
  onPaletteChange: (next: Palette) => void
  busy: boolean
  health: BackendStatus
}

/** 数据源状态：如实反映后端连不连得上，不硬编码「已就绪」。 */
const HEALTH_TEXT: Record<BackendStatus, string> = {
  checking: '正在检查服务…',
  online: '天气 · 景点库 · 汇率 已就绪',
  offline: '未连接到服务端',
}

/** 还在跑时的状态说明。跑完之后的说法由 `statusLabel` 结合 ok 决定。 */
const LIVE_TEXT: Record<SessionStatus, string> = {
  pending: '排队中',
  running: '进行中',
  awaiting_input: '等待你补充信息',
  done: '已完成',
  failed: '运行失败',
  interrupted: '被中断（服务重启）',
}

/**
 * 圆点颜色。
 *
 * 判据是 ``ok`` 而不是 ``status``：一次规划可能「跑完了但模型没产出合规计划」，
 * 那仍然是没成功，该显示红点而不是绿点。
 * ``ok === null`` 表示还在跑 —— 用琥珀色，跟「成功/失败」区分开。
 */
function dotClass(entry: HistoryEntry): string {
  if (entry.ok === true) return 'done'
  if (entry.ok === null) return 'live'
  return 'failed'
}

/**
 * 悬停提示。也不能只看 status ——
 * ``status=done`` 但 ``ok=false`` 的会话说「已完成」是错的。
 */
function statusLabel(entry: HistoryEntry): string {
  if (entry.ok === true) return '已完成'
  if (entry.ok === null) return LIVE_TEXT[entry.status]
  if (entry.status === 'interrupted') return '被中断（服务重启）'
  if (entry.status === 'failed') return '运行失败'
  return '跑完了，但没产出合规计划'
}

export function Rail({
  collapsed,
  canToggle,
  onToggle,
  onHome,
  history,
  activeId,
  onSelect,
  onNew,
  onDelete,
  palette,
  onPaletteChange,
  busy,
  health,
}: Props) {
  return (
    <aside className="rail">
      <div className="brand-row">
        {/* 品牌区本身就是「回首页」的入口 —— 这是几乎所有产品的共同约定，
            不用额外加一个「首页」按钮。
            规划进行中时禁用：这时回首页会看不见正在跑的任务，
            和侧栏「新建任务」保持一致（那个也是 busy 时禁用）。 */}
        <button
          type="button"
          className="brand"
          onClick={onHome}
          disabled={busy || health === 'offline'}
          title={busy ? '规划进行中，完成后再回首页' : '回到首页'}
        >
          <BrandMark />
          <span className="brand-text">
            <span className="brand-name">Cairn</span>
            <span className="brand-sub">任务规划助手</span>
          </span>
        </button>
        {canToggle && (
          <button
            type="button"
            className="rail-toggle"
            onClick={onToggle}
            aria-expanded={!collapsed}
            aria-label={collapsed ? '展开侧栏' : '折叠侧栏'}
            title={collapsed ? '展开侧栏' : '折叠侧栏'}
          >
            <PanelIcon />
          </button>
        )}
      </div>

      <button
        type="button"
        className="rail-btn"
        onClick={onNew}
        disabled={busy || health === 'offline'}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <path
            d="M6 1v10M1 6h10"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>
        <span className="rail-btn-text">新建任务</span>
      </button>

      <div className="rail-group">
        <h3>最近</h3>
        {/* **只有这块滚动。** 品牌、新建任务、外观、数据源都固定不动；
            标题「最近」也留在滚动区外 —— 否则滚下去就看不见分组了。
            实现见 styles.css 的 .rail-list（flex: 1 + min-height: 0）。 */}
        <div className="rail-list">
          {history.length === 0 ? (
            <div className="rail-empty">还没有规划记录</div>
          ) : (
            history.map((entry) => (
              /* 删除按钮是「打开会话」那个按钮的**兄弟**，不是子元素：
                 <button> 里不能嵌 <button>。这样两个都是原生按钮，
                 键盘与焦点语义不用自己模拟，点删除也不会冒泡到「打开会话」。 */
              <div className="rail-row" key={entry.sessionId}>
                <button
                  type="button"
                  className={`rail-item ${dotClass(entry)}${entry.sessionId === activeId ? ' active' : ''}`}
                  onClick={() => onSelect(entry.sessionId)}
                  title={`${entry.task}\n${statusLabel(entry)}`}
                >
                  <span className="dot" />
                  <span className="rail-item-text">{entry.task}</span>
                  <span className="rail-item-initial" aria-hidden="true">
                    {entry.task.slice(0, 1)}
                  </span>
                </button>
                <button
                  type="button"
                  className="rail-del"
                  onClick={() => onDelete(entry.sessionId)}
                  aria-label={`删除「${entry.task}」`}
                  title="删除这条记录"
                >
                  <TrashIcon />
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="rail-foot">
        {/* 外观偏好放在这里而不是光区里：光区一旦有了计划就够不着了 */}
        <div className="k">外观</div>
        <PaletteSwitch value={palette} onChange={onPaletteChange} />
        <div className="k" style={{ marginTop: 14 }}>
          数据源
        </div>
        <div className={`v health-${health}`} title={HEALTH_TEXT[health]}>
          <i />
          <span className="v-text">{HEALTH_TEXT[health]}</span>
        </div>
      </div>
    </aside>
  )
}
