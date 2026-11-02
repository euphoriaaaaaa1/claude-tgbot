// ─── Telegram 线路是否已知不通（纯函数模块，dispatcher.ts 只接线）───────────
// 判定口径：
// - 回包：拿到 Telegram 格式的回应（成功或 Telegram 返回的错误，含 409/429/5xx）；只有网络层失败不算。
// - 进入不通：只能由一次 getMe 探测以网络层失败结束。普通请求的网络层失败只会促成探测，自己不判不通——
//   代理在 Telegram 已投递后掐断响应的偶发情形，不能被当成"线路断了"。
// - 何时探测：a) 距最近一次回包超过 90 秒且最近结束的请求是网络层失败；b) 发送类请求网络层失败后立即探；
//   c) 已不通时每 60 秒再探。未不通时只看 a、b，已不通时只看 c。
// - 恢复：任何请求拿到回包立即恢复。
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export const LINK_DOWN_AFTER_MS = 90_000
export const LINK_REPROBE_MS = 60_000
/** 状态文件 checked_at 与当前时刻相差超过这么多秒（过期或超前），视为 dispatcher 已不在，读方忽略 */
export const LINK_STATE_STALE_SEC = 120

export type NetworkFailureKind = 'poll' | 'send' | 'probe'

export type LinkMonitor = {
  /** 一次回包：立即恢复为通，并清掉待探测请求 */
  recordResponse(nowMs: number): void
  /** 一次网络层失败；'send' 额外登记一次立即探测。本身永远不会让 isDown() 变 true */
  recordNetworkFailure(nowMs: number, kind: NetworkFailureKind): void
  shouldProbe(nowMs: number): boolean
  probeStarted(nowMs: number): void
  /** gotResponse=true 等同 recordResponse；false → 进入不通（已不通则 downSince 保持原值） */
  probeFinished(nowMs: number, gotResponse: boolean): void
  isDown(): boolean
  downSinceMs(): number | null
  /** 是否已有过一次回包或探测结局；只有 startUnknown 创建的监视器才会在一开始为 false */
  isKnown(): boolean
}

const isFiniteNum = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n)

// startUnknown：创建后处于「未知」——isDown() 仍为 false（不拦发送），但 shouldProbe() 立即为真；
// 第一次回包或第一次探测结局结束未知状态。用于启动时线路好坏还没确认的那段时间。
export function createLinkMonitor(opts?: { downAfterMs?: number; reprobeMs?: number; startUnknown?: boolean }): LinkMonitor {
  const pick = (raw: unknown, dflt: number) => (isFiniteNum(raw) && raw >= 0 ? raw : dflt)
  const downAfterMs = pick(opts?.downAfterMs, LINK_DOWN_AFTER_MS)
  const reprobeMs = pick(opts?.reprobeMs, LINK_REPROBE_MS)
  let lastResponseMs = Date.now() // 创建时刻视为一次回包：刚启动不算断线
  let lastEndedFailed = false // 最近一次结束的请求是不是网络层失败（口径 a）
  let probeRequested = false // 发送类网络失败登记的立即探测（口径 b）
  let probing = false
  let lastProbeEndMs = 0
  let downSince: number | null = null
  let known = opts?.startUnknown !== true

  const recordResponse = (nowMs: number) => {
    if (!isFiniteNum(nowMs)) return
    known = true
    if (nowMs > lastResponseMs) lastResponseMs = nowMs
    lastEndedFailed = false
    probeRequested = false
    downSince = null
  }

  return {
    recordResponse,
    recordNetworkFailure(nowMs, kind) {
      if (!isFiniteNum(nowMs)) return
      lastEndedFailed = true
      // 已有探测在进行就不再登记新的；已不通时只按口径 c 重探，不登记
      if (kind === 'send' && !probing && downSince === null) probeRequested = true
    },
    shouldProbe(nowMs) {
      if (!isFiniteNum(nowMs) || probing) return false
      if (!known) return true
      if (downSince !== null) return nowMs - lastProbeEndMs >= reprobeMs
      return probeRequested || (lastEndedFailed && nowMs - lastResponseMs > downAfterMs)
    },
    probeStarted(nowMs) {
      if (!isFiniteNum(nowMs)) return
      probing = true
      probeRequested = false
    },
    probeFinished(nowMs, gotResponse) {
      if (!isFiniteNum(nowMs)) return
      probing = false
      lastProbeEndMs = nowMs
      if (gotResponse === true) {
        recordResponse(nowMs)
        return
      }
      known = true
      lastEndedFailed = true
      if (downSince === null) downSince = nowMs
    },
    isDown() {
      return downSince !== null
    },
    downSinceMs() {
      return downSince
    },
    isKnown() {
      return known
    },
  }
}

/** 发送类方法：名字以 send 开头，或编辑、转发、复制消息。getUpdates / getMe / getFile 等都不算 */
const SEND_LIKE_METHODS = new Set(['editMessageText', 'editMessageCaption', 'editMessageMedia', 'forwardMessage', 'copyMessage'])

/** 线路已知不通时，这个方法要不要在调用 Telegram 之前就拦下（不发网络请求） */
export function shouldBlockSend(method: string, down: boolean): boolean {
  if (down !== true || typeof method !== 'string') return false
  return method.startsWith('send') || SEND_LIKE_METHODS.has(method)
}

/** 线路不通时被拦下的发送抛这个错误：请求根本没发出，确定没送达 */
export class LinkDownError extends Error {
  constructor(message = 'link_down') {
    super(message)
    this.name = 'LinkDownError'
  }
}

// bot 名直接当文件名，只收简单名字，防路径穿越
const BOT_NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/
const stateFile = (dir: string, bot: string): string | null =>
  typeof dir === 'string' && dir !== '' && typeof bot === 'string' && BOT_NAME_RE.test(bot) ? join(dir, bot) : null

/**
 * 不通时写 <dir>/<bot>（一行 `down_since=<秒> checked_at=<秒>`，目录不存在先建，覆盖写），恢复时删。
 * 文件系统错误一律吞掉：状态文件只是给主动开口脚本的提示，写不成不能拖垮 dispatcher。
 */
export function writeLinkState(dir: string, bot: string, down: boolean, downSinceSec: number, nowSec: number): void {
  const file = stateFile(dir, bot)
  if (!file) return
  try {
    if (!down) {
      rmSync(file, { force: true })
      return
    }
    if (!isFiniteNum(downSinceSec) || !isFiniteNum(nowSec)) return
    mkdirSync(dir, { recursive: true })
    // 先写临时文件再改名，读方不会读到半行
    const tmp = `${file}.tmp`
    writeFileSync(tmp, `down_since=${Math.floor(downSinceSec)} checked_at=${Math.floor(nowSec)}\n`)
    renameSync(tmp, file)
  } catch {
    // 吞掉：见函数说明
  }
}

/**
 * 状态文件存在、checked_at 是 1–18 位整数且 nowSec - checked_at 落在 [-120, 120] → 'link_down'；
 * 其余（不存在、乱码、过期、超前、读失败）→ null。下限防时钟回拨后旧文件永远「新鲜」。
 */
export function linkPauseReason(dir: string, bot: string, nowSec: number): string | null {
  const file = stateFile(dir, bot)
  if (!file || !isFiniteNum(nowSec)) return null
  let content: string
  try {
    content = readFileSync(file, 'utf8')
  } catch {
    return null
  }
  const m = /(?:^|\s)checked_at=(\d{1,18})(?=\s|$)/.exec(content)
  if (!m) return null
  const age = nowSec - Number(m[1])
  return age >= -LINK_STATE_STALE_SEC && age <= LINK_STATE_STALE_SEC ? 'link_down' : null
}
