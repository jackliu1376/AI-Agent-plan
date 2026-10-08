/**
 * 计划渲染。
 *
 * 排版原则（对齐图纸）：**目标最大，元信息退后，步骤可扫读**。
 * 依赖关系用可读标签（「依赖 S2」）而不是裸 ID 堆叠 ——
 * 用户不该为了看懂依赖去回滚查找 S2 是什么。
 */

import { memo, useState } from 'react'
import type { Plan, Step } from '../api/types'
import { humanizeSource } from '../lib/toolLabels'

const MAX_SOURCE_CHARS = 34

function Icon({ kind }: { kind: 'info' | 'warn' | 'alert' }) {
  if (kind === 'alert') {
    return (
      <svg width="13" height="13" viewBox="0 0 12 12" fill="none" aria-hidden="true">
        <path d="M6 1.6l4.6 8H1.4l4.6-8z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
        <path d="M6 5v2M6 8.4v.1" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      </svg>
    )
  }
  return (
    <svg width="13" height="13" viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6 5.2v3M6 3.4v.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

function StepRow({ step }: { step: Step }) {
  // 模型会原样写工具名（`get_weather_forecast(city=...)`）。
  // 保留参数（可追溯），把标识符换成人话。
  const raw = step.data_source ?? ''
  const source = humanizeSource(raw)
  const shortSource =
    source.length > MAX_SOURCE_CHARS ? `${source.slice(0, MAX_SOURCE_CHARS)}…` : source

  return (
    <div className="step">
      <span className="step-id">{step.id}</span>
      <div>
        <div className="step-act">{step.action}</div>
        <div className="step-tags">
          {source.length > 0 && (
            <span className="src" title={raw}>
              数据 {shortSource}
            </span>
          )}
          {step.depends_on.length > 0 && <span className="dep">依赖 {step.depends_on.join('、')}</span>}
          {step.eta.length > 0 && <span>耗时 {step.eta}</span>}
          {step.deliverable.length > 0 && <span>产出 {step.deliverable}</span>}
        </div>
      </div>
    </div>
  )
}

interface Props {
  plan: Plan
  markdown: string
  stepCount: number
  /** 第几版；0 表示不显示版本标记（老记录或尚未产生版本） */
  seq?: number
  /** 本次修订改了哪几处（模型自述，走 JSON 旁路 key，不进 Plan schema） */
  revisionSummary?: string
  /** 正在看的是不是历史版本 */
  historical?: boolean
  /** 点「去下面回答」时把焦点送到输入框 */
  onJumpToComposer?: () => void
}

/**
 * 计划正文。
 *
 * **`memo` 是必要的，不是为了好看。** 修订期间每个事件都会让 App 重渲染，
 * 而这份计划的 DOM 相当大（阶段 / 步骤 / 依赖 / 来源标注）——
 * 每次事件都重建一遍，用户看到的就是「进度条一顿一顿地走」。
 *
 * props 里的 `onJumpToComposer` 在 App 侧是 `useCallback(..., [])`，
 * 引用稳定，所以 memo 能真正生效（传内联箭头函数的话它每次都失效）。
 */
const PlanViewBase = memo(function PlanView({
  plan,
  markdown,
  stepCount,
  seq = 0,
  revisionSummary = '',
  historical = false,
  onJumpToComposer,
}: Props) {
  const [showRaw, setShowRaw] = useState(false)

  return (
    <>
      {/* 版本与改动摘要放在**计划顶部**，不放输入框下面 ——
          用户触发修订后眼睛是在文档上的，摘要该跟着内容走。
          放底部的话它就在整个页面最下面，离「改了什么」最远。 */}
      {(seq > 0 || revisionSummary.length > 0) && (
        <div className="plan-version">
          {seq > 0 && <span className="vbadge">第 {seq} 版</span>}
          {revisionSummary.length > 0 && <span className="vsum">{revisionSummary}</span>}
          {historical && <span className="vhint">正在看历史版本</span>}
        </div>
      )}

      <div className="plan-head">
        <h2>{plan.goal}</h2>
        <div className="plan-facts">
          <span>
            <b>{plan.phases.length}</b> 个阶段
          </span>
          <span>
            <b>{stepCount}</b> 个步骤
          </span>
          {plan.total_eta.length > 0 && (
            <span>
              总计 <b>{plan.total_eta}</b>
            </span>
          )}
          {plan.budget_estimate.length > 0 && (
            <span>
              预估 <b>{plan.budget_estimate}</b>
            </span>
          )}
        </div>
      </div>

      {/* 仍需澄清：最需要用户行动的一条，放在最前面 */}
      {plan.clarifications_needed.length > 0 && (
        <div className="note ask">
          <Icon kind="alert" />
          <div>
            <p>
              还需要你确认：
              {onJumpToComposer !== undefined && (
                <button type="button" className="ask-jump" onClick={onJumpToComposer}>
                  去下面回答 ↓
                </button>
              )}
            </p>
            <ul>
              {plan.clarifications_needed.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* 数据时效：数据旧不可怕，用户不知道它旧才可怕 */}
      {(plan.data_freshness.length > 0 || plan.verification_channels.length > 0) && (
        <div className="note fresh">
          <Icon kind="info" />
          <div>
            {plan.data_freshness.length > 0 && <p>{plan.data_freshness}</p>}
            {plan.verification_channels.length > 0 && (
              <p>出行前建议核实：{plan.verification_channels.join('、')}</p>
            )}
          </div>
        </div>
      )}

      {plan.phases.map((phase, index) => (
        <section className="phase" key={`${phase.name}-${index}`}>
          <div className="phase-head">
            <span className="phase-n">{String(index + 1).padStart(2, '0')}</span>
            <span className="phase-name">{phase.name}</span>
            <span className="phase-cnt">{phase.steps.length} 步</span>
          </div>
          {phase.steps.map((step) => (
            <StepRow key={step.id} step={step} />
          ))}
        </section>
      ))}

      {plan.risks.length > 0 && (
        <div className="note warn">
          <Icon kind="alert" />
          <div>
            <p>风险与对策：</p>
            <ul>
              {plan.risks.map((item, index) => (
                <li key={index}>
                  <strong>{item.risk}</strong> —— {item.mitigation}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {plan.assumptions.length > 0 && (
        <div className="note fresh">
          <Icon kind="info" />
          <div>
            <p>本次规划基于这些假设：</p>
            <ul>
              {plan.assumptions.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        <button type="button" className="raw-toggle" onClick={() => setShowRaw((v) => !v)}>
          {showRaw ? '收起 Markdown 原文' : '查看 Markdown 原文'}
        </button>
        {showRaw && <pre className="raw">{markdown}</pre>}
      </div>
    </>
  )
})

export const PlanView = PlanViewBase
