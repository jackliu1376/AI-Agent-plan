/**
 * 计划渲染：阶段 → 步骤，带依赖关系与数据来源标注。
 *
 * 依赖（depends_on）用高亮 tag 展示，让「哪些步骤能并行」一眼可见 ——
 * 这是计划质量的核心，用纯文本是看不出来的。
 */

import { useState } from 'react'
import type { Plan, Step } from '../api/types'

function StepRow({ step }: { step: Step }) {
  return (
    <div className="step">
      <div className="step-id">{step.id}</div>
      <div>
        <div className="step-action">{step.action}</div>
        <div className="step-tags">
          {step.tool !== null && (
            <span className="tag">
              工具 <code>{step.tool}</code>
            </span>
          )}
          {step.depends_on.length > 0 && (
            <span className="tag dep">
              依赖 <code>{step.depends_on.join(', ')}</code>
            </span>
          )}
          {step.eta.length > 0 && <span className="tag">耗时 {step.eta}</span>}
          {step.deliverable.length > 0 && <span className="tag">产出 {step.deliverable}</span>}
          {step.data_source !== null && step.data_source.length > 0 && (
            <span className="tag src">
              数据 <code>{step.data_source}</code>
            </span>
          )}
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
    <section className="panel">
      <div className="panel-head">
        <h2>计划</h2>
        <button type="button" className="raw-toggle" onClick={() => setShowRaw((v) => !v)}>
          {showRaw ? '收起原文' : '查看 Markdown 原文'}
        </button>
      </div>

      <div className="plan-summary">
        <span>
          步骤 <strong>{stepCount}</strong>
        </span>
        {plan.total_eta.length > 0 && (
          <span>
            总耗时 <strong>{plan.total_eta}</strong>
          </span>
        )}
        {plan.budget_estimate.length > 0 && (
          <span>
            预算 <strong>{plan.budget_estimate}</strong>
          </span>
        )}
        <span>
          阶段 <strong>{plan.phases.length}</strong>
        </span>
      </div>

      <div className="goal">
        <h3>{plan.goal}</h3>
      </div>

      {plan.assumptions.length > 0 && (
        <div className="notes">
          <h4>假设</h4>
          <ul>
            {plan.assumptions.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {plan.clarifications_needed.length > 0 && (
        <div className="notes warning">
          <h4>仍需澄清</h4>
          <ul>
            {plan.clarifications_needed.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {plan.phases.map((phase, index) => (
        <div className="phase" key={index}>
          <div className="phase-head">
            {index + 1}. {phase.name}
          </div>
          <div className="steps">
            {phase.steps.map((step) => (
              <StepRow key={step.id} step={step} />
            ))}
          </div>
        </div>
      ))}

      {plan.risks.length > 0 && (
        <div className="notes">
          <h4>风险与对策</h4>
          <ul>
            {plan.risks.map((item, i) => (
              <li key={i}>
                <strong>{item.risk}</strong> —— {item.mitigation}
              </li>
            ))}
          </ul>
        </div>
      )}

      {showRaw && <pre className="raw">{markdown}</pre>}
    </section>
  )
}
