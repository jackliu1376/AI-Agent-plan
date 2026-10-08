/**
 * 时间格式化 —— 侧栏列表和主区页头共用。
 *
 * 今天 / 昨天用相对说法，更早退回月-日。**不用「3 天前」这类说法** ——
 * 那要心算，而这两处都是拿来「扫」的。
 */

/** Unix 毫秒 → 「今天 10:04」/「昨天」/「10-05」。 */
export function fmtWhen(ts: number, now: Date = new Date()): string {
  const d = new Date(ts)
  const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()

  if (ts >= dayStart) {
    const hh = String(d.getHours()).padStart(2, '0')
    const mm = String(d.getMinutes()).padStart(2, '0')
    return `今天 ${hh}:${mm}`
  }
  if (ts >= dayStart - 86_400_000) return '昨天'

  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${mm}-${dd}`
}
