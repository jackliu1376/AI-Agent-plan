/**
 * 计划渲染。
 *
 * 排版原则（对齐图纸）：**目标最大，元信息退后，步骤可扫读**。
 * 依赖关系用可读标签（「依赖 S2」）而不是裸 ID 堆叠 ——
 * 用户不该为了看懂依赖去回滚查找 S2 是什么。
 */

import { useState } from 'react'
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
}

export function PlanView({ plan, markdown, stepCount }: Props) {
  const [showRaw, setShowRaw] = useState(false)

  return (
    <>
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
            <p>还需要你确认：</p>
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
}
