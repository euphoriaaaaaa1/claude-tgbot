/**
 * provider_watch.ts — 盯 settings.json，provider 真变了就让 worker 换 provider 重生。
 *
 * 为什么盯目录不盯文件：cc-switch / 多数编辑器是「写临时文件再 rename 覆盖」，
 * 盯文件本身的 watch 句柄在 rename 后就聋了（inode 换了），盯父目录才收得到。
 * 轮询兜底 10s：同 worker-manager 的 "Windows fs.watch 语义差异保险" 节奏，
 * fs.watch 在 Windows / 网络盘 / 容器挂载上都可能漏事件，靠轮询保底。
 *
 * 事件不直接动手：先攒 debounce 窗口（默认 1.2s）再评估一次，连写多次只重启一次。
 * dispatcher 本体不重启，只重启 claude 子进程（重启逻辑在 worker-manager，这里只发信号）。
 */
import { createHash } from 'crypto'
import { readFileSync, watch, type FSWatcher } from 'fs'
import { join, dirname, basename } from 'path'
import { homedir } from 'os'
import { providerSig, shouldRefresh, debounceGate } from './provider_sync'

export const DEBOUNCE_MS = 1200
export const POLL_MS = 10_000

// 监听目标；worker 起子进程时读的必须是同一个文件（worker-manager.settingsEnv 也走这个函数）。
// 环境变量是测试/多配置的注入点：绝不在测试里指向真实 ~/.claude/settings.json。
export function settingsPath(): string {
  return process.env.CLAUDE_SETTINGS_PATH || join(homedir(), '.claude', 'settings.json')
}

export type ProviderWatchHandle = {
  stop(): void
  /** 立刻评估一次（跳过防抖计时器），给测试与手动触发用 */
  evaluate(): void
}

export type ProviderWatchOptions = {
  /** provider 真变了：调用方负责重启 worker（复用 worker-manager 既有崩溃重启） */
  onProviderChanged: () => void
  path?: string
  debounceMs?: number
  pollMs?: number
  /** 日志只允许布尔 + 原因码，永远不打字段值 */
  log?: (line: string) => void
  now?: () => number
}

export function startProviderWatch(opts: ProviderWatchOptions): ProviderWatchHandle {
  const path = opts.path ?? settingsPath()
  const debounceMs = opts.debounceMs ?? DEBOUNCE_MS
  const pollMs = opts.pollMs ?? POLL_MS
  const now = opts.now ?? Date.now
  const log = opts.log ?? ((line: string) => process.stderr.write(`provider-sync: ${line}\n`))

  // 读不到/读到半截 → 返回空串，providerSig 会给出空签名，按「读不出配置」处理，不崩不误触发
  const readRaw = (): string => {
    try { return readFileSync(path, 'utf8') } catch { return '' }
  }
  // 只为判断「文件动过没有」留指纹，不在内存里留配置明文（settings.json 含真实凭证）
  const fingerprint = (raw: string): string =>
    createHash('sha256').update(raw).digest('hex').slice(0, 16)

  const raw0 = readRaw()
  let prevSig = providerSig(raw0)   // 启动基线：只记不触发
  let prevFp = fingerprint(raw0)
  let lastTrigger = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  let stopped = false

  // 已有 pending 计时器就不再顺延：一串连写合并成一次评估，也防止「轮询周期 < 防抖窗口」时
  // 计时器被无限重置永远评估不了。
  function schedule(): void {
    if (stopped || timer) return
    timer = setTimeout(evaluate, debounceMs)
    timer.unref?.()
  }

  function evaluate(): void {
    timer = null
    if (stopped) return
    const raw = readRaw()
    const fp = fingerprint(raw)
    const fileChanged = fp !== prevFp
    prevFp = fp
    const nextSig = providerSig(raw)

    if (!shouldRefresh(prevSig, nextSig)) {
      // 空签名不覆盖基线：文件短暂消失/写到一半时若把基线冲成空，等它回来带着新 provider
      // 就会被判成「首次基线」而漏掉这次切换。
      if (!fileChanged) return                       // 轮询空转，不刷日志
      if (nextSig === '') log('refresh=false reason=unreadable')
      else if (prevSig === '') { prevSig = nextSig; log('refresh=false reason=baseline') }
      else log('refresh=false reason=irrelevant_change')
      return
    }
    if (!debounceGate(lastTrigger, now(), debounceMs)) {
      log('refresh=false reason=debounced')
      schedule()                                     // 压这一拍，等窗口过了再评估
      return
    }
    prevSig = nextSig
    lastTrigger = now()
    log('refresh=true reason=provider_changed')
    try { opts.onProviderChanged() } catch { log('refresh=true reason=restart_failed') }
  }

  let watcher: FSWatcher | null = null
  try {
    watcher = watch(dirname(path), (_ev, fname) => {
      if (fname && basename(String(fname)) !== basename(path)) return
      schedule()
    })
    // 目录被删/句柄失效不能把 dispatcher 带崩，轮询继续兜底
    watcher.on('error', () => { try { watcher?.close() } catch {} ; watcher = null })
    watcher.unref?.()
  } catch { /* 目录还不存在等：全靠轮询 */ }

  const poll = setInterval(schedule, pollMs)
  poll.unref?.()

  return {
    stop() {
      stopped = true
      if (timer) { clearTimeout(timer); timer = null }
      clearInterval(poll)
      try { watcher?.close() } catch {}
      watcher = null
    },
    evaluate,
  }
}
