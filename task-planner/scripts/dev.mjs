#!/usr/bin/env node
/**
 * 一条命令拉起开发环境：后端（FastAPI :8000）+ 前端（Vite :5173）。
 *
 * 为什么不用「开两个终端」或者一个朴素的 .bat：
 *
 * 1. **Windows 上杀进程要杀整棵树。** `uv run` 会派生子进程，`child.kill()`
 *    只杀得掉 wrapper，`task-planner-web.exe` 会留下来占着端口和文件句柄 ——
 *    本项目已经被这个坑了一整天（`uv sync` 报 `os error 32` 就是它）。
 *    这里用 `taskkill /F /T /PID` 连子树一起收。
 *
 * 2. **启动前先清理残留。** 上次没退干净的进程会让这次启动「看起来成功了」
 *    但其实是旧进程在服务 —— 你会改了半天代码发现没生效。
 *
 * 3. **输出加前缀。** 两个进程的输出混在一起，不加前缀根本分不清谁在报错。
 *
 * 用法：npm run dev:all    （或双击 start-dev.bat）
 */

import { spawn, spawnSync } from 'node:child_process'
import { createInterface } from 'node:readline'
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..') // task-planner/
const FRONTEND = resolve(ROOT, 'frontend')

const API_PORT = Number(process.env.WEB_PORT ?? 8000)
const WEB_PORT = Number(process.env.VITE_PORT ?? 5173)

const isWindows = process.platform === 'win32'

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

const C = {
  dim: (s) => `\x1b[2m${s}\x1b[0m`,
  red: (s) => `\x1b[31m${s}\x1b[0m`,
  green: (s) => `\x1b[32m${s}\x1b[0m`,
  yellow: (s) => `\x1b[33m${s}\x1b[0m`,
  cyan: (s) => `\x1b[36m${s}\x1b[0m`,
}

function log(message) {
  process.stdout.write(`${message}\n`)
}

/** 给子进程的每一行输出加前缀。返回一个 promise，可等待某个信号出现。 */
function pipeWithPrefix(stream, prefix, color, onLine) {
  const rl = createInterface({ input: stream })
  rl.on('line', (line) => {
    // 空行不浪费一次前缀
    process.stdout.write(line.trim() === '' ? '\n' : `${color(prefix)} ${line}\n`)
    onLine?.(line)
  })
}

/** 等待某个条件成立，超时返回 false。 */
function waitFor(check, { timeoutMs = 30000, intervalMs = 300 } = {}) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs
    const tick = () => {
      if (check()) return resolve(true)
      if (Date.now() > deadline) return resolve(false)
      setTimeout(tick, intervalMs)
    }
    tick()
  })
}

/** 杀掉整棵进程树 —— Windows 上必须用 taskkill /T，否则会留下孤儿进程。 */
function killTree(child, signal = 'SIGTERM') {
  if (child === null || child.exitCode !== null || child.signalCode !== null) return
  try {
    if (isWindows) {
      spawnSync('taskkill', ['/F', '/T', '/PID', String(child.pid)], { stdio: 'ignore' })
    } else {
      child.kill(signal)
    }
  } catch {
    /* 进程可能已经没了 */
  }
}

/**
 * 找出正在监听某个端口的进程 PID。
 *
 * 为什么需要它：`taskkill /T` 靠父子关系找子孙，但 **npm 退出后 Vite 会被
 * 重新挂到别的父进程下**，`/T` 就够不着了 —— 实测会出现「脚本说已停止，
 * 但 5173 还在监听」。按端口找 PID 是 Windows 上唯一可靠的办法。
 */
function pidsOnPort(port) {
  if (!isWindows) return []
  const out = spawnSync('netstat', ['-ano'], { encoding: 'utf8' }).stdout ?? ''
  const pids = new Set()
  for (const line of out.split('\n')) {
    if (!line.includes('LISTENING')) continue
    const parts = line.trim().split(/\s+/)
    // 列：协议 本地地址 外部地址 状态 PID
    const local = parts[1] ?? ''
    const pid = parts[parts.length - 1] ?? ''
    if (!local.endsWith(`:${port}`)) continue
    if (/^\d+$/.test(pid) && pid !== '0') pids.add(pid)
  }
  return [...pids]
}

/** 把监听在指定端口上的进程全部杀掉。 */
function killPort(port) {
  const pids = pidsOnPort(port)
  for (const pid of pids) {
    spawnSync('taskkill', ['/F', '/T', '/PID', pid], { stdio: 'ignore' })
  }
  return pids.length
}

/** 端口上是否已经有东西在监听。 */
async function portInUse(port) {
  try {
    const resp = await fetch(`http://127.0.0.1:${port}/`, {
      signal: AbortSignal.timeout(1200),
    })
    return resp.status > 0
  } catch {
    return false
  }
}

/** 清理上一次没退干净的进程（按镜像名 + 按端口，双保险）。 */
function cleanStaleProcesses() {
  if (!isWindows) return
  const names = ['task-planner-web.exe', 'task-planner-desktop.exe']
  for (const name of names) {
    const probe = spawnSync('tasklist', ['/FI', `IMAGENAME eq ${name}`], { encoding: 'utf8' })
    if (probe.stdout?.includes(name)) {
      log(C.yellow(`  ⚠ ${name} 仍在运行，先停掉它`))
      spawnSync('taskkill', ['/F', '/IM', name], { stdio: 'ignore' })
    }
  }
  // 按镜像名杀不到 Vite（它就叫 node.exe），得按端口找。
  //
  // 注意措辞：端口上占着的**不一定是残留进程**，也可能是另一个正在跑的实例
  // （比如你自己开的一个终端）。同一个端口本来就绑不了两次，所以这里必须
  // 让位 —— 但要如实说清是「停掉了占用者」，而不是把它说成垃圾。
  for (const port of [API_PORT, WEB_PORT]) {
    const killed = killPort(port)
    if (killed > 0) {
      log(C.yellow(`  ⚠ 端口 ${port} 已被占用，已停止占用它的进程（${killed} 个）`))
    }
  }
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

const children = []
let shuttingDown = false

function shutdown(code = 0) {
  if (shuttingDown) return
  shuttingDown = true
  log('')
  log(C.dim('正在停止服务…'))
  for (const child of children) killTree(child)
  // taskkill /T 靠父子关系，但 npm 退出后 Vite 会被重新挂到别的父进程下 ——
  // 实测会出现「脚本说已停止，但 5173 还在监听」。所以再按端口兜一次。
  for (const port of [API_PORT, WEB_PORT]) killPort(port)
  cleanStaleProcesses()
  log(C.dim('已停止。'))
  process.exit(code)
}

async function main() {
  log(C.cyan('▶ Cairn 开发环境'))
  log(C.dim(`  后端 http://127.0.0.1:${API_PORT}   前端 http://localhost:${WEB_PORT}`))
  log('')

  if (!existsSync(resolve(FRONTEND, 'node_modules'))) {
    log(C.red('✗ frontend/node_modules 不存在，请先运行：'))
    log(C.dim('    cd frontend && npm install'))
    process.exit(1)
  }

  cleanStaleProcesses()

  // 端口占用会让「启动成功」变成假象 —— 先问清楚
  for (const [port, label] of [
    [API_PORT, '后端'],
    [WEB_PORT, '前端'],
  ]) {
    if (await portInUse(port)) {
      log(C.yellow(`⚠ 端口 ${port}（${label}）已有服务在监听`))
      log(C.dim(`  如果不是本脚本启动的，先停掉它；否则可能连到了旧进程。`))
    }
  }

  // ---- 后端 ----
  const api = spawn('uv', ['run', 'task-planner-web'], {
    cwd: ROOT,
    env: { ...process.env, WEB_PORT: String(API_PORT) },
    shell: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  children.push(api)
  pipeWithPrefix(api.stdout, '[api]', C.cyan)
  pipeWithPrefix(api.stderr, '[api]', C.cyan)

  api.on('exit', (code) => {
    if (shuttingDown) return
    log(C.red(`✗ 后端退出（code=${code}）`))
    shutdown(1)
  })

  // 等后端就绪再起前端 —— 否则首屏的 /api 请求全是代理错误，噪音很大
  const apiReady = await waitFor(async () => {
    try {
      const resp = await fetch(`http://127.0.0.1:${API_PORT}/api/health`, {
        signal: AbortSignal.timeout(1000),
      })
      return resp.ok
    } catch {
      return false
    }
  })
  log(apiReady ? C.green('✓ 后端就绪') : C.yellow('⚠ 后端 30 秒内未就绪，仍继续启动前端'))

  // ---- 前端 ----
  let viteReady = false
  const web = spawn('npm', ['run', 'dev'], {
    cwd: FRONTEND,
    env: { ...process.env, VITE_API_TARGET: `http://127.0.0.1:${API_PORT}` },
    shell: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  children.push(web)
  // Vite 就绪时会打印 "ready in XXX ms" —— 用它当信号，
  // 否则会在前端真正起来之前就说「都起来了」，用户点开页面还是白屏
  const onViteLine = (line) => {
    if (line.includes('ready in')) viteReady = true
  }
  pipeWithPrefix(web.stdout, '[web]', C.green, onViteLine)
  pipeWithPrefix(web.stderr, '[web]', C.green, onViteLine)

  web.on('exit', (code) => {
    if (shuttingDown) return
    log(C.red(`✗ 前端退出（code=${code}）`))
    shutdown(1)
  })

  await waitFor(() => viteReady, { timeoutMs: 20000 })
  log('')
  if (viteReady) {
    log(`  ${C.green('✓ 都起来了')}  →  ${C.cyan(`http://localhost:${WEB_PORT}`)}`)
  } else {
    log(`  ${C.yellow('⚠ 前端 20 秒内未见就绪信号')}  仍可尝试 ${C.cyan(`http://localhost:${WEB_PORT}`)}`)
  }
  log(C.dim('  按 Ctrl+C 停止全部服务'))
  log('')
}

process.on('SIGINT', () => shutdown(0))
process.on('SIGTERM', () => shutdown(0))

main().catch((err) => {
  log(C.red(`✗ 启动失败：${err.message}`))
  shutdown(1)
})
