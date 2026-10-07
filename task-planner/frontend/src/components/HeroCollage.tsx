/**
 * 空状态的光区拼贴。
 *
 * 这块**只起装饰作用**，不承载任何信息 —— 所以内容是通用的旅行符号
 * （药丸卡 / 照片 / 大数字白卡 / 深色玻璃卡 / 渐变面板），不是产品说明图。
 * 早先做过两版「有语义的图示」（收敛式连线、依赖图），都推翻了：
 * 空状态没有信息要传达，把装饰做成说明只会两边都不像。
 *
 * 三个必须守住的实现细节：
 *
 * 1. **不要 preserve-3d。** 倾斜加在**整块台面**上就够了。逐张卡片做 3D 分层
 *    时，Chromium 会在层数变多后随机丢层（实测 8 张卡只画出 1~3 张，
 *    而且每次跑还不一样）。这里全部用 2D 变换。
 * 2. **不要 backdrop-filter。** 和上面的问题同源，去掉后稳定。
 * 3. **每张卡的角度写在 `--r` 上，关键帧里要带上它** ——
 *    否则漂浮动画一跑，卡片就被「扶正」了，角度全丢。
 */

import type { CSSProperties } from 'react'

/** 一张卡的位置与动画参数。`r` 是它自己的角度，`d` 是入场延迟。 */
interface Piece {
  cls: string
  style: CSSProperties & Record<string, string | number>
  body: React.ReactNode
}

const PIECES: Piece[] = [
  {
    cls: 'fc panel',
    style: { left: 522, top: 40, width: 154, height: 198, zIndex: 1, '--r': '7deg', '--d': '.40s', '--dur': '12s', '--rot': '.6deg' },
    body: (
      <span className="bolt">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M13.2 2.2 4.6 13.6h6.2L9.6 21.8l8.6-11.4h-6.2z" />
        </svg>
      </span>
    ),
  },
  {
    cls: 'fc pill p1',
    style: { left: 8, top: 64, width: 176, zIndex: 2, '--r': '-7deg', '--d': '.10s', '--dur': '9s' },
    body: (
      <>
        <span className="av">陈</span>
        <span className="nm">成都 2 天</span>
        <span className="tg">08</span>
      </>
    ),
  },
  {
    cls: 'fc pill p2',
    style: { left: 24, top: 126, width: 158, zIndex: 3, '--r': '-5deg', '--d': '.20s', '--dur': '10.5s' },
    body: (
      <>
        <span className="av">林</span>
        <span className="nm">杭州 3 天</span>
        <span className="tg">12</span>
      </>
    ),
  },
  {
    cls: 'fc photo',
    style: { left: 234, top: 22, width: 166, height: 206, zIndex: 3, '--r': '-3deg', '--d': '.30s', '--dur': '11s' },
    body: <img src="/travel.jpg" alt="" />,
  },
  {
    cls: 'fc stat',
    style: { left: 96, top: 154, width: 158, zIndex: 4, '--r': '-4deg', '--d': '.16s', '--dur': '9.5s' },
    body: (
      <>
        <span className="tk a">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M2.6 18.4l5.6-8 4 5.4 2.4-3 7 5.6z" />
          </svg>
        </span>
        <div className="lb">预算</div>
        <div className="nb">¥1,280</div>
        <span className="pl">人均</span>
      </>
    ),
  },
  {
    cls: 'fc stat',
    style: { left: 372, top: 8, width: 158, zIndex: 4, '--r': '2.5deg', '--d': '.24s', '--dur': '12.5s' },
    body: (
      <>
        <span className="tk b">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M2.4 11.6 21.6 3.2l-8.4 19.2-2.3-8.1z" />
          </svg>
        </span>
        <div className="lb">行程</div>
        <div className="nb">3 天</div>
        <span className="pl hot">待出发</span>
      </>
    ),
  },
  {
    cls: 'fc glass',
    style: { left: 330, top: 150, width: 178, zIndex: 5, '--r': '-2deg', '--d': '.34s', '--dur': '10s' },
    body: (
      <>
        <span className="tk">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M4.5 12.5l4.5 4.5L19.5 6.5" />
          </svg>
        </span>
        <span className="tx">
          <span className="k1">已确认</span>
          <span className="k2">5 项</span>
        </span>
        <span className="add">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
            <path d="M8 3.5v9M3.5 8h9" />
          </svg>
        </span>
      </>
    ),
  },
  {
    cls: 'fc badge',
    style: { left: 252, top: 200, width: 40, height: 40, zIndex: 6, '--r': '0deg', '--d': '.44s', '--dur': '13s' },
    body: (
      <i>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 2.6l2.6 6.1 6.6.5-5 4.3 1.5 6.5L12 16.6l-5.7 3.4 1.5-6.5-5-4.3 6.6-.5z" />
        </svg>
      </i>
    ),
  },
]

export function HeroCollage() {
  return (
    /* 三层结构：frame 决定「占多大地方」，wrap 负责等比缩放，stage 是真正的台面。
       窄屏时只要改 --fit，frame 的宽高会跟着算，不会留空隙也不会溢出。 */
    <div className="stage-frame" aria-hidden="true">
      <div className="stage-wrap">
        <div className="stage">
          {PIECES.map((piece, index) => (
            <div className={piece.cls} style={piece.style} key={index}>
              {piece.body}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
