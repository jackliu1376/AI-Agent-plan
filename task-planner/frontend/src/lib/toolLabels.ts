/**
 * 工具名 → 人话标签。
 *
 * 两个地方要用：
 * 1. 「已查证」列表的左列
 * 2. 计划步骤的 `data_source`（模型会原样写工具名，如
 *    `get_weather_forecast(city=成都, start_date=2026-10-10)`）
 *
 * `data_source` **不能删** —— 它是「每个事实都有出处」这条原则的载体。
 * 但给用户看函数名是开发者视角，这里只做**展示层替换**：
 * 保留参数（可追溯），把标识符换成人话。
 */

export const TOOL_LABELS: Record<string, string> = {
  get_weather_forecast: '天气',
  query_attractions_db: '景点库',
  query_attraction_realtime: '景点核对',
  estimate_route: '城际交通',
  query_transit_options: '班次',
  convert_currency: '汇率',
  fetch_webpage: '网页',
  parse_budget_csv: '预算表',
  save_itinerary: '落盘',
  ask_user_clarification: '澄清',
}

export function labelOf(tool: string): string {
  return TOOL_LABELS[tool] ?? tool
}

/**
 * 把自由文本里的工具函数名替换成人话标签。
 *
 * 只替换**已知**的工具名，未知的保持原样 —— 宁可露出一个陌生标识符，
 * 也不要猜错它的含义。
 */
export function humanizeSource(text: string): string {
  let result = text
  for (const [tool, label] of Object.entries(TOOL_LABELS)) {
    if (result.includes(tool)) {
      result = result.split(tool).join(label)
    }
  }
  return result
}
