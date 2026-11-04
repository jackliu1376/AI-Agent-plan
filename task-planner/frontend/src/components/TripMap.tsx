/**
 * 行程地图。
 *
 * 三件事按优先级：
 *
 * 1. **能画就画真地图。** 用高德 JS API（与 POI 查询同一坐标系，零转换）。
 * 2. **画不了就降级，不白屏。** 未配置 key、脚本加载失败、key 被域名白名单
 *    拒绝 —— 任一情况都回落到自绘 SVG 示意图。它没有街道底图，但「哪几个
 *    景点挨着、哪天在哪片区域」这个核心信息一点没少。
 * 3. **没有坐标就整个不渲染。** 非旅游任务不该出现一块空地图占位置。
 *
 * 坐标是 GCJ-02，直接喂给高德，**不做任何偏移校正**。
 */

import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { getMapConfig } from '../api/client'
import type { MapConfig, PoiMarker } from '../api/types'

/** 日次配色。第 1 天用主色，其余取项目已有的点缀色 + 两个冷色补充。 */
const PIN_COLORS = ['#1b1a18', '#b8555f', '#4d7c5c', '#9a7420', '#5b6b8c', '#8a5a7a']
const PIN_FALLBACK = '#1b1a18'

function pinColor(index: number): string {
  return PIN_COLORS[index % PIN_COLORS.length] ?? PIN_FALLBACK
}

// ---------------------------------------------------------------------------
// 高德 JS API（只声明用到的那几个成员）
// ---------------------------------------------------------------------------

interface AMapOverlay {
  setMap(map: unknown): void
}

interface AMapMapInstance {
  add(overlay: AMapOverlay): void
  remove(overlay: AMapOverlay): void
  /** ``avoid`` 是 [上,下,左,右] 的留白像素，不给的话点会贴着容器边缘被裁掉。 */
  setFitView(overlays?: unknown, immediately?: boolean, avoid?: number[]): void
  destroy(): void
  /** 高德在瓦片和图层都就绪后触发 `complete`。画覆盖物必须等它。 */
  on(event: string, handler: () => void): void
}

interface AMapNamespace {
  Map: new (el: HTMLElement, opts: Record<string, unknown>) => AMapMapInstance
  Marker: new (opts: Record<string, unknown>) => AMapOverlay
  Polyline: new (opts: Record<string, unknown>) => AMapOverlay
  Pixel: new (x: number, y: number) => unknown
}

declare global {
  interface Window {
    AMap?: AMapNamespace
    _AMapSecurityConfig?: { securityJsCode: string }
  }
}

/**
 * 加载高德脚本。**全模块单例** —— 每次挂载都插一个 `<script>` 的话，
 * 切换会话几次就会重复加载多份 SDK，后加载的会覆盖先前的全局变量。
 */
let loader: Promise<AMapNamespace> | null = null

/**
 * 地图配置也是模块级单例。
 *
 * StrictMode 下组件会挂载两次，不缓存就会发两个 `GET /api/map/config`。
 * 这个配置在一次会话里不会变，缓存没有副作用。
 */
let configPromise: Promise<MapConfig> | null = null

function loadConfig(): Promise<MapConfig> {
  configPromise ??= getMapConfig().catch((err: unknown) => {
    // 失败不缓存，下次挂载允许重试
    configPromise = null
    throw err
  })
  return configPromise
}

/**
 * 高德脚本加载超时。
 *
 * `onload` / `onerror` 是仅有的两个出口，但**它们不一定都会触发** ——
 * 请求被广告拦截器拦掉、DNS 挂起、代理返回一个不完整的响应，都可能让
 * Promise 永远 pending，界面就卡在「地图加载中」不动了。
 * 6 秒后强制失败，走降级示意图 —— 高德脚本在国内通常 1–2 秒就绪，
 * 超过 6 秒基本可以判定这次加载不会成功了。
 */
const SCRIPT_TIMEOUT_MS = 6000

function loadAmap(key: string, securityCode: string): Promise<AMapNamespace> {
  if (window.AMap !== undefined) return Promise.resolve(window.AMap)
  if (loader !== null) return loader

  loader = new Promise<AMapNamespace>((resolve, reject) => {
    // securityJsCode 必须在脚本加载**之前**挂到 window 上，
    // 否则 SDK 初始化时读不到，会抛 INVALID_USER_SCODE。
    if (securityCode.length > 0) {
      window._AMapSecurityConfig = { securityJsCode: securityCode }
    }

    const script = document.createElement('script')
    let settled = false

    const fail = (reason: string) => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      // 置空以便下次挂载时重试
      loader = null
      reject(new Error(reason))
    }

    const timer = window.setTimeout(
      () => fail(`高德 JS API 加载超时（${SCRIPT_TIMEOUT_MS / 1000}s）`),
      SCRIPT_TIMEOUT_MS,
    )

    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}`
    script.async = true
    script.onload = () => {
      if (settled) return
      if (window.AMap !== undefined) {
        settled = true
        window.clearTimeout(timer)
        resolve(window.AMap)
        return
      }
      fail('高德脚本已加载，但没有暴露 AMap 全局变量')
    }
    script.onerror = () => fail('高德 JS API 加载失败（可能被浏览器扩展拦截）')
    document.head.appendChild(script)
  })
  return loader
}

// ---------------------------------------------------------------------------
// 几何
// ---------------------------------------------------------------------------

function haversineKm(a: PoiMarker, b: PoiMarker): number {
  const rad = Math.PI / 180
  const dLat = (b.lat - a.lat) * rad
  const dLon = (b.lon - a.lon) * rad
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2
  return 2 * 6371 * Math.asin(Math.sqrt(h))
}

/** 按天把相邻景点连起来，累加直线距离。 */
function routeKm(pois: PoiMarker[]): number {
  const groups = new Map<string, PoiMarker[]>()
  for (const poi of pois) {
    const key = poi.day
    const list = groups.get(key)
    if (list === undefined) groups.set(key, [poi])
    else list.push(poi)
  }
  let total = 0
  for (const list of groups.values()) {
    for (let i = 1; i < list.length; i += 1) {
      const prev = list[i - 1]
      const curr = list[i]
      if (prev !== undefined && curr !== undefined) total += haversineKm(prev, curr)
    }
  }
  return total
}

// ---------------------------------------------------------------------------
// 降级示意图
// ---------------------------------------------------------------------------

interface FallbackProps {
  pois: PoiMarker[]
  days: string[]
  activeDay: string
}

/**
 * 等距圆柱投影 + 中纬度余弦修正。
 *
 * 不修正经度的话，杭州（北纬 30°）的东西向距离会被拉长约 15%，
 * 看起来像景点都在一条横线上。
 */
function FallbackMap({ pois, days, activeDay }: FallbackProps) {
  const W = 600
  const H = 300
  const PAD = 44

  const placed = useMemo(() => {
    const lons = pois.map((p) => p.lon)
    const lats = pois.map((p) => p.lat)
    const minLon = Math.min(...lons)
    const maxLon = Math.max(...lons)
    const minLat = Math.min(...lats)
    const maxLat = Math.max(...lats)
    const kx = Math.cos((((minLat + maxLat) / 2) * Math.PI) / 180)
    const rawX = (maxLon - minLon) * kx
    const rawY = maxLat - minLat
    // 只有一个点、或所有点在同一经纬度时，跨度是 0 —— 直接除会得到 NaN。
    // 这时把点放到画布中心，而不是靠 PAD 把它们挤到左上角。
    const flatX = rawX < 1e-6
    const flatY = rawY < 1e-6

    const counter = new Map<string, number>()
    return pois.map((poi) => {
      const n = (counter.get(poi.day) ?? 0) + 1
      counter.set(poi.day, n)
      const x = flatX
        ? W / 2
        : PAD + (((poi.lon - minLon) * kx) / rawX) * (W - PAD * 2)
      const y = flatY ? H / 2 : PAD + ((maxLat - poi.lat) / rawY) * (H - PAD * 2)
      return { poi, n, x, y }
    })
  }, [pois])

  const dim = (day: string) => activeDay.length > 0 && day !== activeDay
  // 没有日次标记时用一个空字符串分组，让点仍然连成一条线 ——
  // 「顺序」比「分组」更基础，不能因为没识别出 D1/D2 就丢掉。
  const groups = days.length > 0 ? days : ['']

  return (
    <svg
      className="tripmap-fallback"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label="景点相对位置示意图"
    >
      <rect width={W} height={H} fill="var(--surface-2)" />
      {groups.map((day) => {
        const points = placed.filter((item) => item.poi.day === day)
        if (points.length < 2) return null
        return (
          <polyline
            key={`line-${day}`}
            points={points.map((p) => `${p.x},${p.y}`).join(' ')}
            fill="none"
            stroke={pinColor(days.indexOf(day))}
            strokeWidth={1.5}
            strokeDasharray="5 4"
            opacity={dim(day) ? 0.2 : 0.55}
          />
        )
      })}
      {placed.map(({ poi, n, x, y }) => {
        // 标签居中在 pin 下方，但贴近左右边缘时会溢出画布 ——
        // 这时改成左对齐 / 右对齐，而不是把文字硬推回中间（那样会和 pin 脱节）。
        const anchor = x < 70 ? 'start' : x > W - 70 ? 'end' : 'middle'
        const labelX = x < 70 ? x - 9 : x > W - 70 ? x + 9 : x
        const label = poi.name.length > 8 ? `${poi.name.slice(0, 8)}…` : poi.name
        return (
          <g key={`${poi.name}-${poi.lon}-${poi.lat}`} opacity={dim(poi.day) ? 0.25 : 1}>
            <title>{poi.name}</title>
            <circle cx={x} cy={y} r={11} fill={pinColor(days.indexOf(poi.day))} />
            <text x={x} y={y + 4} textAnchor="middle" fontSize={11} fill="#ffffff">
              {n}
            </text>
            <text x={labelX} y={y + 26} textAnchor={anchor} fontSize={11} fill="var(--ink-2)">
              {label}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

interface Props {
  pois: PoiMarker[]
}

export const TripMap = memo(function TripMap({ pois }: Props) {
  const [activeDay, setActiveDay] = useState('')
  const [mode, setMode] = useState<'loading' | 'live' | 'fallback'>('loading')
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<AMapMapInstance | null>(null)
  const amapRef = useRef<AMapNamespace | null>(null)
  const overlaysRef = useRef<AMapOverlay[]>([])
  const [mapReady, setMapReady] = useState(false)
  // 兜底定时器在异步回调里创建，但 cleanup 是同步的 —— 用 ref 传出去才能清掉
  const cleanupTimerRef = useRef<() => void>(() => {})
  // 建图 effect 的依赖是空数组（地图只建一次），但要读第一个点的坐标当初始中心，
  // 所以用 ref 转一道，避免把 pois 塞进依赖导致每次修订都重建地图。
  const poisRef = useRef(pois)
  poisRef.current = pois

  const days = useMemo(() => {
    const seen: string[] = []
    for (const poi of pois) {
      if (poi.day.length > 0 && !seen.includes(poi.day)) seen.push(poi.day)
    }
    return seen.sort()
  }, [pois])

  const visible = useMemo(
    () => (activeDay.length === 0 ? pois : pois.filter((p) => p.day === activeDay)),
    [pois, activeDay],
  )

  // 建图：只跑一次。切日次不重建地图，只重画覆盖物。
  useEffect(() => {
    let cancelled = false

    void (async () => {
      let config
      try {
        config = await loadConfig()
      } catch {
        if (!cancelled) setMode('fallback')
        return
      }
      if (cancelled) return
      if (!config.enabled) {
        setMode('fallback')
        return
      }

      try {
        const amap = await loadAmap(config.key, config.security_code)
        if (cancelled) return
        const el = containerRef.current
        if (el === null) return

        const first = poisRef.current[0]
        amapRef.current = amap
        const map = new amap.Map(el, {
          zoom: 12,
          // **center 不能省。** 不传的话高德用默认中心（不在行程城市），
          // marker 的绝对定位会算到几万像素外，整张图看起来是空的。
          center: first === undefined ? [116.397, 39.909] : [first.lon, first.lat],
          viewMode: '2D',
          resizeEnable: true,
          // 灰白风格与页面底色同一调性。默认的彩色底图（黄快速路、绿地、
          // 蓝水系）太抢眼，会把 pin 和连线压下去。
          mapStyle: 'amap://styles/whitesmoke',
        })
        mapRef.current = map

        // 覆盖物必须等 `complete` 再画：地图未就绪时 add 的 marker 位置会算错，
        // setFitView 也不生效。但瓦片挂了 complete 可能永远不来，加个兜底定时器，
        // 宁可底图空着也要把点画出来。
        let settled = false
        const settle = () => {
          if (settled || cancelled) return
          settled = true
          setMapReady(true)
          setMode('live')
        }
        map.on('complete', settle)
        const timer = window.setTimeout(settle, 4000)
        cleanupTimerRef.current = () => window.clearTimeout(timer)
      } catch {
        if (!cancelled) setMode('fallback')
      }
    })()

    return () => {
      cancelled = true
      cleanupTimerRef.current()
      mapRef.current?.destroy()
      mapRef.current = null
      amapRef.current = null
      overlaysRef.current = []
      setMapReady(false)
    }
  }, [])

  // 画覆盖物：切日次、修订出新版时重画。
  useEffect(() => {
    const map = mapRef.current
    const amap = amapRef.current
    if (!mapReady || map === null || amap === null) return

    for (const overlay of overlaysRef.current) map.remove(overlay)
    overlaysRef.current = []

    const drawn: AMapOverlay[] = []
    const groups = new Map<string, PoiMarker[]>()
    for (const poi of visible) {
      const list = groups.get(poi.day)
      if (list === undefined) groups.set(poi.day, [poi])
      else list.push(poi)
    }

    for (const [day, list] of groups) {
      const color = pinColor(days.indexOf(day))

      if (list.length > 1) {
        const line = new amap.Polyline({
          path: list.map((p) => [p.lon, p.lat]),
          strokeColor: color,
          strokeWeight: 2,
          strokeOpacity: 0.5,
          strokeStyle: 'dashed',
          strokeDasharray: [6, 4],
          lineJoin: 'round',
          zIndex: 50,
        })
        map.add(line)
        drawn.push(line)
      }

      list.forEach((poi, index) => {
        // 用 DOM 构造而不是 innerHTML：景点名来自外部接口，
        // 不该有机会被当成标记解析。
        const dot = document.createElement('div')
        dot.className = 'tripmap-pin'
        dot.style.background = color
        dot.textContent = String(index + 1)

        const marker = new amap.Marker({
          position: [poi.lon, poi.lat],
          content: dot,
          offset: new amap.Pixel(-13, -13),
          title: poi.name,
          zIndex: 100 + index,
        })
        map.add(marker)
        drawn.push(marker)
      })
    }

    overlaysRef.current = drawn
    // 60px 留白：marker 直径 26px，贴边时会被容器裁掉一半。
    if (drawn.length > 0) map.setFitView(drawn, false, [70, 70, 70, 70])
  }, [mapReady, visible, days])

  if (pois.length === 0) return null

  const totalKm = routeKm(pois)

  return (
    <section className="tripmap" aria-label="行程地图">
      <header className="tripmap-head">
        <span className="tripmap-title">行程地图</span>
        <span className="tripmap-facts">
          {pois.length} 个景点
          {days.length > 1 && ` · 跨 ${days.length} 天`}
          {totalKm > 0.5 && ` · 直线约 ${Math.round(totalKm)} 公里`}
        </span>
        {days.length > 1 && (
          <div className="tripmap-days">
            <button
              type="button"
              className={activeDay === '' ? 'on' : undefined}
              onClick={() => setActiveDay('')}
            >
              全部
            </button>
            {days.map((day) => (
              <button
                key={day}
                type="button"
                className={activeDay === day ? 'on' : undefined}
                onClick={() => setActiveDay(day)}
              >
                {day}
              </button>
            ))}
          </div>
        )}
      </header>

      <div className="tripmap-canvas">
        {/* 容器必须常驻**且可见**：高德初始化时要读容器尺寸，
            在 display:none 的节点里会拿到 0×0，地图直接渲染不出来。
            所以只在确定降级时才隐藏，加载阶段用遮罩盖住即可。 */}
        <div
          ref={containerRef}
          className="tripmap-live"
          hidden={mode === 'fallback'}
          aria-hidden={mode !== 'live'}
        />
        {mode === 'loading' && <div className="tripmap-loading">地图加载中…</div>}
        {mode === 'fallback' && <FallbackMap pois={pois} days={days} activeDay={activeDay} />}
      </div>

      <footer className="tripmap-foot">
        <div className="tripmap-legend">
          {days.length === 0 && (
            <span>
              <i style={{ background: PIN_FALLBACK }} />
              景点位置
            </span>
          )}
          {/* 带上每天的景点数 —— 光有颜色对照，用户还得自己数点 */}
          {days.map((day) => (
            <span key={day}>
              <i style={{ background: pinColor(days.indexOf(day)) }} />
              {day} · {pois.filter((p) => p.day === day).length} 处
            </span>
          ))}
        </div>
        <span className="tripmap-source">
          {mode === 'live' ? '高德地图' : '相对位置示意'} · 坐标为 GCJ-02
        </span>
      </footer>
    </section>
  )
})
