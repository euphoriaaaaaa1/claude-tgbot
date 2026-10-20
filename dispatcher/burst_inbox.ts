/**
 * 凑一波再递 · IO 层：按 chat 攒波、到点把这一波合并成一个投递文件。
 *
 * 决策全在 burst_plan.ts（纯函数），这里只做"读原件 / 写合并件 / 删原件 / 交给投递队列"。
 * 依赖全部注入（时钟、落盘目录、投递回调、日志），所以能拿假时钟 + 临时目录直接白盒测。
 *
 * 合并件落在 <channel>/.burst（刻意避开 inbox/，否则 drainInbox 会把它当新消息再收一遍）。
 * 原件只在合并件原子落盘之后才删；崩在中间最坏重投一次，任何路径都不丢消息。
 * 日志只写条数 / chat_id / 原因码，绝不写正文。
 */
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs'
import { basename, join } from 'path'
import { decideBurst, mergeInboxJsons, type BurstAction, type InMsg } from './burst_plan'
import { isInternalMessage } from './channel_wrap'

export type BurstCfg = { windowMs: number; maxMs: number }

export type BurstDeps = BurstCfg & {
  destDir: string                        // 合并件落盘目录（生产 = <channel>/.burst）
  deliver: (path: string) => void        // 投递一条原件/合并件（生产 = 进 WorkerManager 队列）
  log?: (line: string) => void
  now?: () => number                     // 假时钟注入口
}

export type BurstCollector = {
  enabled: boolean                       // windowMs<=0 → false，调用方走原路径（行为回归现状）
  arrive: (path: string) => boolean      // true=已收进波次层；false=认不出（坏 JSON / 无 chat_id），调用方照旧处理
  tick: () => void                       // 定时器驱动（生产 500ms 粒度）
  waves: () => number                    // 诊断/测试：当前在攒的波数
}

export const DEFAULT_BURST: BurstCfg = { windowMs: 5000, maxMs: 12000 }

/** access.json 读 burstWindowMs / burstMaxMs；缺失、非数、读不到 → 缺省值。字符串数字也认。 */
export function readBurstCfg(channelDir: string): BurstCfg {
  const num = (v: unknown, dflt: number): number => {
    const n = typeof v === 'string' && v.trim() !== '' ? Number(v) : v
    return typeof n === 'number' && Number.isFinite(n) ? n : dflt
  }
  let raw: Record<string, unknown> = {}
  try { raw = JSON.parse(readFileSync(join(channelDir, 'access.json'), 'utf8')) ?? {} } catch { raw = {} }
  const windowMs = num(raw.burstWindowMs, DEFAULT_BURST.windowMs)
  // 封顶不可能早于窗口，否则每条一到就被"封顶"冲掉，等于关了防抖
  return { windowMs, maxMs: Math.max(num(raw.burstMaxMs, DEFAULT_BURST.maxMs), windowMs) }
}

/** 不参与合并的消息：媒体、peer bot 发言、内部合成消息（H-27 先看 meta.synthetic 标记；
 *  '[' 正文前缀作为兜底，兼容未带标记的旧 writer）。合并件的 meta 取首条，这些消息各自有
 *  专属 meta / 前缀语义，混进去就串了。 */
function unmergeable(meta: Record<string, any>): boolean {
  if (meta.image_path || meta.attachment_kind) return true
  if (meta.is_bot_sender === true) return true
  if (isInternalMessage(meta)) return true
  return typeof meta.text === 'string' && meta.text.startsWith('[')
}

const senderOf = (meta: Record<string, any>): string => String(meta.from_id ?? '')

export function createBurstCollector(deps: BurstDeps): BurstCollector {
  const { windowMs, maxMs, destDir, deliver } = deps
  const now = deps.now ?? Date.now
  const log = deps.log ?? (() => {})
  const enabled = windowMs > 0
  // chatId → 当前波（到达序）+ 波首的发送人。群里两个人 5s 内各说一句不能并成一条，
  // 否则合并件的 meta 取首条 = 把后面那人的话按在前一个人头上。
  const waves = new Map<string, { msgs: InMsg[]; from: string }>()
  // path → 该条 InMsg：写失败重试时要把整条（含正文）放回队首，光有 path 不够。
  const byPath = new Map<string, InMsg>()

  function remember(m: InMsg): void { byPath.set(m.path, m) }
  function forget(path: string): void { byPath.delete(path) }

  function readMeta(path: string): Record<string, any> | null {
    try {
      const o = JSON.parse(readFileSync(path, 'utf8'))
      return o && typeof o === 'object' && !Array.isArray(o) ? o : null
    } catch { return null }
  }

  function store(chatId: string, msgs: InMsg[], from: string): void {
    if (msgs.length === 0) waves.delete(chatId)
    else waves.set(chatId, { msgs, from })
  }

  /** 原子落盘合并件：写 tmp + rename 成功之后才删源文件。任何一步抛 → 源文件一个都还没删。 */
  function writeMergedFile(merged: string, items: string[]): string {
    mkdirSync(destDir, { recursive: true })
    const stem = basename(items[0]!).replace(/\.json$/i, '').replace(/[^a-zA-Z0-9_-]/g, '_')
    let dst = join(destDir, `${stem}-burst.json`)
    for (let n = 1; existsSync(dst); n++) dst = join(destDir, `${stem}-burst-${n}.json`)
    const tmp = `${dst}.tmp`
    writeFileSync(tmp, merged)
    renameSync(tmp, dst)                       // 原子生效这一行之后，源文件才可删
    for (const p of items) rmSync(p, { force: true })
    return dst
  }

  /**
   * 投递一波。返回 true=已投出（或无需投），false=合并件写失败需整波重试。
   * 两类失败分开处置（H-22）：读/解析原件失败（内容坏了，重试也没用）→ 降级逐条原样投，绝不丢；
   * 写/rename 失败（目标目录只读等）→ 源文件一个不删，整条放回队首重试，记 merge_write_failed。
   */
  function flushPaths(chatId: string, paths: string[]): boolean {
    const items = paths.filter(p => typeof p === 'string' && existsSync(p))
    if (items.length === 0) return true
    if (items.length === 1) { deliver(items[0]!); forget(items[0]!); return true }  // 一条原件直投
    let merged: string
    try {
      merged = mergeInboxJsons(items.map(p => readFileSync(p, 'utf8')))
    } catch (e) {
      // 解析不了 → 重试也不会好，按原顺序逐条投（内容坏了但绝不丢）
      log(`burst_degrade chat=${chatId} count=${items.length} reason=${e instanceof Error ? e.name : 'unknown'}`)
      for (const p of items) { deliver(p); forget(p) }
      return true
    }
    try {
      const dst = writeMergedFile(merged, items)
      log(`burst_merged chat=${chatId} count=${items.length}`)
      for (const p of items) forget(p)
      deliver(dst)
      return true
    } catch {
      log(`merge_write_failed n=${items.length}`)
      return false
    }
  }

  /** 写失败：把这一波（含随后该单独投的 single）整条放回该 chat 波次队首，下一轮 tick 重试。 */
  function requeue(chatId: string, paths: string[], from: string): void {
    const back = paths.map(p => byPath.get(p)).filter((m): m is InMsg => !!m)
    if (back.length === 0) return
    const set = new Set(back.map(m => m.path))
    const rest = (waves.get(chatId)?.msgs ?? []).filter(m => !set.has(m.path))
    waves.set(chatId, { msgs: [...back, ...rest], from })
  }

  function run(chatId: string, action: BurstAction, from: string): void {
    if (action.kind === 'hold') return
    const ok = flushPaths(chatId, action.paths)
    if (action.kind === 'flushThenSingle') {
      if (ok) { deliver(action.single); forget(action.single) }
      else requeue(chatId, [...action.paths, action.single], from)
      return
    }
    if (!ok) requeue(chatId, action.paths, from)
  }

  function arrive(path: string): boolean {
    if (!enabled) return false
    const meta = readMeta(path)
    if (!meta) return false
    const chatId = String(meta.chat_id ?? '')
    if (!chatId) return false
    const cur = waves.get(chatId)
    // fs.watch 同一个文件可能连报两次事件；重复收进波里 = 合并后正文出现两遍
    if (cur?.msgs.some(m => m.path === path)) return true
    const msg: InMsg = {
      path,
      ts: now(),                          // 波窗口按到达时刻算，与 tick 同一把时钟（meta.ts 是消息自身时刻，不通用）
      chatId,
      text: '',                           // 正文不进内存，只在 flush 时读一次原件
      hasMedia: unmergeable(meta) || (cur ? senderOf(meta) !== cur.from : false),
    }
    remember(msg)                         // 写失败重试时要能整条放回波首
    const r = decideBurst(cur?.msgs ?? [], { kind: 'arrive', msg }, windowMs, maxMs)
    const from = cur?.from ?? senderOf(meta)
    store(chatId, r.wave, from)
    run(chatId, r.action, from)
    return true
  }

  function tick(): void {
    if (waves.size === 0) return
    const t = now()
    for (const [chatId, w] of [...waves]) {
      const r = decideBurst(w.msgs, { kind: 'timer', nowMs: t }, windowMs, maxMs)
      store(chatId, r.wave, w.from)
      run(chatId, r.action, w.from)
    }
  }

  return { enabled, arrive, tick, waves: () => waves.size }
}
