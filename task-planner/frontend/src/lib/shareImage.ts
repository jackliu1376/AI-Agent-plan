/**
 * 把计划画成一张分享图。
 *
 * 为什么不用 `html2canvas` 之类的 DOM 截图库
 * ------------------------------------------
 * 这个前端是**零运行时依赖**的（只有 react / react-dom），为一张图引入
 * 50KB+ 的截图库不划算。而且计划数据是**结构化的**（`Plan` 里有
 * goal / phases / steps / total_eta / budget_estimate），直接画比截 DOM 更可控：
 * 可以去掉所有交互元素、按分享场景重排、保证不同屏幕下长得一样。
 *
 * 两遍绘制
 * --------
 * 画布高度取决于内容（15 步和 30 步差很多），所以先「空跑一遍」量出总高，
 * 再建画布正式画。测量走一个独立的 canvas —— 正式绘制时 `ctx` 传 `null`，
 * 所有绘制函数都要能接受 null 并只返回高度。
 */

import type { Plan } from '../api/types'

/** 逻辑宽度。1080 是微信长图的常见宽度，压缩后仍清晰。 */
const W = 1080
const PAD = 72
const CONTENT_W = W - PAD * 2

/** 和产品同一套色值（浅色主题）。分享图固定用浅色 —— 深色图在微信里很怪。 */
const C = {
  bg: '#faf9f8',
  card: '#ffffff',
  line: '#eceae6',
  ink: '#1b1a18',
  ink2: '#6d6a63',
  ink3: '#a6a29a',
}

const SANS =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif"

/** 2x 渲染，视网膜屏和微信压缩后都清楚。 */
const DPR = 2

/** 单个步骤最多显示几行 —— 太长的步骤会把图撑得很难看。 */
const STEP_MAX_LINES = 3

type Ctx = CanvasRenderingContext2D | null

// ---------------------------------------------------------------------------
// 测量
// ---------------------------------------------------------------------------

let _measure: CanvasRenderingContext2D | null = null

function measureCtx(): CanvasRenderingContext2D {
  if (_measure === null) {
    _measure = document.createElement('canvas').getContext('2d')!
  }
  return _measure
}

/**
 * 折行。中文按字断，英文/数字按词断 —— 纯按字符断会把 `Beijing` 拆成
 * `Beij` / `ing`，很难看。
 */
function wrap(text: string, maxWidth: number, font: string): string[] {
  const m = measureCtx()
  m.font = font
  const tokens = text.match(/[A-Za-z0-9][A-Za-z0-9._%+-]*|\s+|[^\s]/gu) ?? []
  const lines: string[] = []
  let cur = ''

  for (const t of tokens) {
    const next = cur + t
    if (cur !== '' && m.measureText(next).width > maxWidth) {
      lines.push(cur.replace(/\s+$/, ''))
      cur = /^\s+$/.test(t) ? '' : t
    } else {
      cur = next
    }
  }
  const tail = cur.replace(/\s+$/, '')
  if (tail !== '') lines.push(tail)
  return lines.length > 0 ? lines : ['']
}

function clip(text: string, maxChars: number): string {
  return text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text
}

/**
 * 从 ``total_eta`` 里抽出适合放进指标卡的短值。
 *
 * 模型写的是「行前准备 7.5h + 行程 7 天 + 收尾 2h」这种完整句子 ——
 * 直接塞进 234px 宽的格子会被截成「行前准…」，等于没显示。
 * 优先取含「天」的片段，那是行程里最关心的量。
 */
function shortDuration(text: string): string {
  const t = text.trim()
  if (t === '') return '—'
  const days = t.match(/[\d.]+\s*天/)
  if (days !== null) return days[0].replace(/\s+/g, ' ')
  const hours = t.match(/[\d.]+\s*(?:小时|h)/i)
  if (hours !== null) return hours[0].replace(/\s+/g, ' ')
  return clip(t, 8)
}

/** 同理：``budget_estimate`` 只取金额本身，「人均」那层含义交给标签行说。
    实测「人均 ¥3100–3950」在 222px 的格子里放不下，缩到 20px 又太糊。 */
function shortBudget(text: string): string {
  const t = text.trim()
  if (t === '') return '—'
  const per = t.match(/人均[约]?\s*(¥\s?[\d,]+(?:\s*[–—~-]\s*¥?\s?[\d,]+)?)/)
  if (per !== null && per[1] !== undefined) return per[1].replace(/\s+/g, '')
  const any = t.match(/¥\s?[\d,]+(?:\s*[–—~-]\s*¥?\s?[\d,]+)?/)
  return any !== null ? any[0].replace(/\s+/g, '') : clip(t, 9)
}

function textWidth(text: string, font: string): number {
  const m = measureCtx()
  m.font = font
  return m.measureText(text).width
}

// ---------------------------------------------------------------------------
// 绘制原语（ctx 为 null 时只返回高度）
// ---------------------------------------------------------------------------

interface TextOpts {
  size: number
  weight?: number
  color?: string
  /** 行高倍数 */
  lh?: number
  maxWidth?: number
  x?: number
  /** 最多显示几行，超出加省略号 */
  maxLines?: number
}

function textBlock(ctx: Ctx, y: number, raw: string, o: TextOpts): number {
  const font = `${o.weight ?? 400} ${o.size}px ${SANS}`
  const maxW = o.maxWidth ?? CONTENT_W
  let lines = wrap(raw, maxW, font)

  if (o.maxLines !== undefined && lines.length > o.maxLines) {
    lines = lines.slice(0, o.maxLines)
    const last = lines[lines.length - 1] ?? ''
    lines[lines.length - 1] = `${last.slice(0, Math.max(1, last.length - 1))}…`
  }

  const step = o.size * (o.lh ?? 1.5)
  if (ctx !== null) {
    ctx.font = font
    ctx.fillStyle = o.color ?? C.ink
    ctx.textBaseline = 'top'
    lines.forEach((line, i) => ctx.fillText(line, o.x ?? PAD, y + i * step))
  }
  return lines.length * step
}

function rule(ctx: Ctx, y: number, x = PAD, w = CONTENT_W): number {
  if (ctx !== null) {
    ctx.fillStyle = C.line
    ctx.fillRect(x, y, w, 1)
  }
  return 1
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

function ellipse(ctx: CanvasRenderingContext2D, cx: number, cy: number, rx: number, ry: number) {
  ctx.beginPath()
  ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2)
  ctx.fill()
}

/** 品牌标记：三块叠石（和侧栏 Rail.tsx 里的 BrandMark 同一造型）。 */
function brandMark(ctx: Ctx, x: number, y: number, size: number): void {
  if (ctx === null) return
  ctx.fillStyle = C.ink
  roundRect(ctx, x, y, size, size, size * 0.31)
  ctx.fill()

  ctx.fillStyle = '#ffffff'
  const cx = x + size / 2
  ellipse(ctx, cx, y + size * 0.70, size * 0.233, size * 0.113)
  ellipse(ctx, cx, y + size * 0.508, size * 0.175, size * 0.096)
  ellipse(ctx, cx, y + size * 0.346, size * 0.113, size * 0.075)
}

// ---------------------------------------------------------------------------
// 各区块
// ---------------------------------------------------------------------------

function drawHeader(ctx: Ctx, y: number, at: Date): number {
  const MARK = 44
  brandMark(ctx, PAD, y, MARK)

  if (ctx !== null) {
    ctx.font = `600 26px ${SANS}`
    ctx.fillStyle = C.ink
    ctx.textBaseline = 'top'
    ctx.fillText('Cairn', PAD + MARK + 16, y + 1)

    ctx.font = `400 19px ${SANS}`
    ctx.fillStyle = C.ink3
    ctx.fillText('任务规划助手', PAD + MARK + 16, y + 29)

    // 右上角日期
    const stamp = fmtDate(at)
    ctx.font = `400 19px ${SANS}`
    ctx.fillStyle = C.ink3
    ctx.textAlign = 'right'
    ctx.fillText(stamp, W - PAD, y + 12)
    ctx.textAlign = 'left'
  }
  return MARK
}

function drawGoal(ctx: Ctx, y: number, goal: string): number {
  return textBlock(ctx, y, goal, { size: 46, weight: 600, lh: 1.35, maxLines: 3 })
}

interface Metric {
  value: string
  label: string
}

function drawMetrics(ctx: Ctx, y: number, metrics: Metric[]): number {
  const H = 132
  const gap = 16
  const n = Math.max(metrics.length, 1)
  const cellW = (CONTENT_W - gap * (n - 1)) / n

  metrics.forEach((m, i) => {
    const x = PAD + i * (cellW + gap)
    if (ctx !== null) {
      ctx.fillStyle = C.card
      roundRect(ctx, x, y, cellW, H, 18)
      ctx.fill()
      ctx.strokeStyle = C.line
      ctx.lineWidth = 1
      roundRect(ctx, x + 0.5, y + 0.5, cellW - 1, H - 1, 18)
      ctx.stroke()
    }
    // 数值自适应字号：从 40 逐步缩到刚好放得下（下限 22，再小就糊了）。
    // 硬编码几档字号试过，「人均 ¥3100–3950」这种长度总是差一点。
    let vsize = 40
    while (vsize > 22 && textWidth(m.value, `600 ${vsize}px ${SANS}`) > cellW - 44) {
      vsize -= 2
    }
    textBlock(ctx, y + 30, m.value, {
      size: vsize, weight: 600, lh: 1.1, maxWidth: cellW - 40, x: x + 22, maxLines: 1,
    })
    textBlock(ctx, y + H - 40, m.label, {
      size: 19, color: C.ink3, lh: 1.2, maxWidth: cellW - 40, x: x + 22, maxLines: 1,
    })
  })
  return H
}

function drawPhase(ctx: Ctx, y: number, name: string, index: number, total: number): number {
  let cur = y
  cur += textBlock(ctx, cur, `${index + 1} / ${total}　${name}`, {
    size: 27, weight: 600, lh: 1.4, maxLines: 1,
  })
  cur += 14
  cur += rule(ctx, cur)
  return cur - y + 26
}

function drawStep(ctx: Ctx, y: number, no: number, action: string, eta: string): number {
  const NUM_W = 40
  const x = PAD + NUM_W + 6
  const maxW = CONTENT_W - NUM_W - 6

  if (ctx !== null) {
    // 序号圈
    ctx.fillStyle = C.line
    ctx.beginPath()
    ctx.arc(PAD + 15, y + 19, 15, 0, Math.PI * 2)
    ctx.fill()

    ctx.font = `500 17px ${SANS}`
    ctx.fillStyle = C.ink2
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(String(no), PAD + 15, y + 20)
    ctx.textAlign = 'left'
    ctx.textBaseline = 'top'
  }

  let h = textBlock(ctx, y, action, {
    size: 23, lh: 1.5, maxWidth: maxW, x, maxLines: STEP_MAX_LINES,
  })

  if (eta.trim() !== '') {
    h += 6
    h += textBlock(ctx, y + h, eta, { size: 18, color: C.ink3, lh: 1.3, maxWidth: maxW, x, maxLines: 1 })
  }
  return h + 22
}

function drawNote(ctx: Ctx, y: number, label: string, body: string): number {
  let cur = y
  cur += textBlock(ctx, cur, label, { size: 19, weight: 600, color: C.ink3, lh: 1.4, maxLines: 1 })
  cur += 8
  cur += textBlock(ctx, cur, body, { size: 19, color: C.ink2, lh: 1.65 })
  return cur - y
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

export interface ShareMeta {
  model: string
  at: Date
}

function fmtDate(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/**
 * 跑一遍完整布局。`ctx` 为 `null` 时只累加高度，不做任何绘制 ——
 * 这是「先量后画」的关键。
 */
function layout(ctx: Ctx, plan: Plan, meta: ShareMeta): number {
  let y = PAD

  y += drawHeader(ctx, y, meta.at)
  y += 52

  y += drawGoal(ctx, y, plan.goal)
  y += 44

  const steps = plan.phases.reduce((n, p) => n + p.steps.length, 0)
  y += drawMetrics(ctx, y, [
    { value: String(plan.phases.length), label: '个阶段' },
    { value: String(steps), label: '个步骤' },
    { value: shortDuration(plan.total_eta), label: '行程时长' },
    { value: shortBudget(plan.budget_estimate), label: '人均预算' },
  ])
  y += 60

  let no = 0
  plan.phases.forEach((phase, i) => {
    y += drawPhase(ctx, y, clip(phase.name, 24), i, plan.phases.length)
    for (const step of phase.steps) {
      no += 1
      y += drawStep(ctx, y, no, step.action, step.eta)
    }
    y += 24
  })

  y += 12
  y += rule(ctx, y)
  y += 34

  // 时效声明必须带上 —— 这张图会被转发到别处，看的人不知道数据是什么时候的。
  if (plan.data_freshness.trim() !== '') {
    y += drawNote(ctx, y, '数据时效', plan.data_freshness)
    y += 26
  }
  if (plan.verification_channels.length > 0) {
    y += drawNote(ctx, y, '核实渠道', plan.verification_channels.join(' · '))
    y += 26
  }

  y += rule(ctx, y)
  y += 24

  const foot = `由 Cairn 生成 · ${fmtDate(meta.at)} · ${meta.model}`
  y += textBlock(ctx, y, foot, { size: 18, color: C.ink3, lh: 1.4, maxLines: 1 })

  return y + PAD
}

/** 把计划渲染成 PNG。 */
export async function renderPlanImage(plan: Plan, meta: ShareMeta): Promise<Blob> {
  const height = Math.ceil(layout(null, plan, meta))

  const canvas = document.createElement('canvas')
  canvas.width = W * DPR
  canvas.height = height * DPR
  const ctx = canvas.getContext('2d')
  if (ctx === null) throw new Error('无法创建画布上下文')

  ctx.scale(DPR, DPR)
  ctx.fillStyle = C.bg
  ctx.fillRect(0, 0, W, height)

  layout(ctx, plan, meta)

  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob === null) reject(new Error('图片生成失败'))
      else resolve(blob)
    }, 'image/png')
  })
}

/** 复制图片到剪贴板。浏览器不支持时抛错，由调用方降级为下载。 */
export async function copyImageToClipboard(blob: Blob): Promise<void> {
  if (typeof ClipboardItem === 'undefined' || navigator.clipboard?.write === undefined) {
    throw new Error('浏览器不支持复制图片')
  }
  await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
}

/** 触发下载。 */
export function downloadImage(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  // 立刻 revoke 会让部分浏览器来不及开始下载
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/** 文件名：用计划目标的前若干字。中英文标点都要清掉 —— 目标句子里
    几乎必然带逗号和顿号，留着会生成「Cairn-4-人从成都自驾出发，2026-…」这种名字。 */
export function imageFilename(plan: Plan, at: Date): string {
  const safe = plan.goal
    .replace(/[\\/:*?"<>|\s]+/g, '-')
    .replace(/[，。、；：！？（）【】「」《》·—–~]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 24)
    .replace(/-$/, '')
  return `Cairn-${safe !== '' ? safe : '计划'}-${fmtDate(at)}.png`
}
