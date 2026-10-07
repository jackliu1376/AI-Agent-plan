/**
 * 分享按钮：把当前计划画成一张图，复制或保存。
 *
 * 为什么不放「复制链接」：这个服务绑的是 `127.0.0.1`，链接发出去对方打不开 ——
 * 摆一个点了没用的入口比不放更糟。
 *
 * 面板向上还是向下：按钮在页面右上角，所以菜单往下弹；同时做了「点外面 /
 * Esc 关闭」，和 Composer 的版本菜单保持一致。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Plan } from '../api/types'
import {
  copyImageToClipboard,
  downloadImage,
  imageFilename,
  renderPlanImage,
} from '../lib/shareImage'

function ShareIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 1.6v8.6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <path
        d="M4.9 4.6L8 1.6l3.1 3"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M3.2 8.4v4c0 .8.6 1.4 1.4 1.4h6.8c.8 0 1.4-.6 1.4-1.4v-4"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  )
}

function CopyIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect x="5.4" y="5.4" width="8" height="8" rx="2" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M10.6 5.4V4.2c0-1.1-.9-2-2-2H4.2c-1.1 0-2 .9-2 2v4.4c0 1.1.9 2 2 2h1.2"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 2v8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      <path
        d="M4.9 7.2L8 10.3l3.1-3.1"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M3 13.2h10" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  )
}

interface Props {
  plan: Plan
  model: string
}

type Notice = { kind: 'ok' | 'err'; text: string } | null

export function ShareButton({ plan, model }: Props) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)
  const holderRef = useRef<HTMLDivElement | null>(null)
  const buttonRef = useRef<HTMLButtonElement | null>(null)

  // 点外面 / Esc 关闭
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!holderRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(false)
      // 焦点还给按钮，否则键盘用户会掉到 body 上
      buttonRef.current?.focus()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // 提示 3 秒后自动消失
  useEffect(() => {
    if (notice === null) return
    const t = setTimeout(() => setNotice(null), 3000)
    return () => clearTimeout(t)
  }, [notice])

  const withImage = useCallback(
    async (action: (blob: Blob) => Promise<void> | void, okText: string) => {
      setBusy(true)
      try {
        const blob = await renderPlanImage(plan, { model, at: new Date() })
        await action(blob)
        setNotice({ kind: 'ok', text: okText })
        setOpen(false)
      } catch (err) {
        setNotice({
          kind: 'err',
          text: err instanceof Error ? err.message : '生成图片失败',
        })
      } finally {
        setBusy(false)
      }
    },
    [plan, model],
  )

  const onCopy = () =>
    void withImage(async (blob) => {
      try {
        await copyImageToClipboard(blob)
      } catch {
        // 剪贴板写图片在部分浏览器/非 https 下会被拒 —— 退化成下载，
        // 总比什么都不发生强
        downloadImage(blob, imageFilename(plan, new Date()))
        throw new Error('浏览器不允许直接复制图片，已改为下载')
      }
    }, '已复制，可直接粘贴到微信或飞书')

  const onDownload = () =>
    void withImage(
      (blob) => downloadImage(blob, imageFilename(plan, new Date())),
      '图片已保存到下载目录',
    )

  return (
    <div className="share" ref={holderRef}>
      <button
        ref={buttonRef}
        type="button"
        className="share-btn"
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        aria-haspopup="menu"
        aria-expanded={open}
        title="把这份计划分享给别人"
      >
        <ShareIcon />
        <span>{busy ? '生成中…' : '分享'}</span>
      </button>

      {open && (
        <div className="share-menu" role="menu">
          <div className="share-menu-head">分享这份计划</div>
          <button type="button" className="share-item" role="menuitem" onClick={onCopy} disabled={busy}>
            <CopyIcon />
            <span className="share-item-text">
              <b>复制为图片</b>
              <em>粘到微信、飞书或文档里</em>
            </span>
          </button>
          <button type="button" className="share-item" role="menuitem" onClick={onDownload} disabled={busy}>
            <DownloadIcon />
            <span className="share-item-text">
              <b>保存为图片</b>
              <em>下载一张竖版长图</em>
            </span>
          </button>
        </div>
      )}

      {notice !== null && (
        <div className={`share-notice ${notice.kind}`} role="status">
          {notice.text}
        </div>
      )}
    </div>
  )
}
