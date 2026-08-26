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

/** 不参与合并的消息：媒体、peer bot 发言、内部合成消息（[self-initiate]/[director]/… 一律 '[' 开头）。
 *  合并件的 meta 取首条，这些消息各自有专属 meta / 前缀语义，混进去就串了。 */
function unmergeable(meta: Record<string, any>): boolean {
  if (meta.image_path || meta.attachment_kind) return true
  if (meta.is_bot_sender === true) return true
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

  /** 合并落盘：先原子写合并件，再删原件。崩在中间 → 原件还在（最坏重投一次，绝不丢）。 */
  function writeMerged(items: string[]): string {
    const merged = mergeInboxJsons(items.map(p => readFileSync(p, 'utf8')))
    mkdirSync(destDir, { recursive: true })
    const stem = basename(items[0]!).replace(/\.json$/i, '').replace(/[^a-zA-Z0-9_-]/g, '_')
    let dst = join(destDir, `${stem}-burst.json`)
    for (let n = 1; existsSync(dst); n++) dst = join(destDir, `${stem}-burst-${n}.json`)
    const tmp = `${dst}.tmp`
    writeFileSync(tmp, merged)
    renameSync(tmp, dst)
    for (const p of items) rmSync(p, { force: true })
    return dst
  }

  function flushPaths(chatId: string, paths: string[]): void {
    const items = paths.filter(p => typeof p === 'string' && existsSync(p))
    if (items.length === 0) return
    if (items.length === 1) { deliver(items[0]!); return }   // 一条没什么好合的，原件直投
    try {
      const dst = writeMerged(items)
      log(`burst_merged chat=${chatId} count=${items.length}`)
      deliver(dst)
    } catch (e) {
      // 合并/落盘任何一步炸了都降级：按原顺序逐条投，一条不少
      log(`burst_degrade chat=${chatId} count=${items.length} reason=${e instanceof Error ? e.name : 'unknown'}`)
      for (const p of items) deliver(p)
    }
  }

  function run(chatId: string, action: BurstAction): void {
    if (action.kind === 'hold') return
    flushPaths(chatId, action.paths)
    if (action.kind === 'flushThenSingle') deliver(action.single)
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
    const r = decideBurst(cur?.msgs ?? [], { kind: 'arrive', msg }, windowMs, maxMs)
    store(chatId, r.wave, cur?.from ?? senderOf(meta))
    run(chatId, r.action)
    return true
  }

  function tick(): void {
    if (waves.size === 0) return
    const t = now()
    for (const [chatId, w] of [...waves]) {
      const r = decideBurst(w.msgs, { kind: 'timer', nowMs: t }, windowMs, maxMs)
      store(chatId, r.wave, w.from)
      run(chatId, r.action)
    }
  }

  return { enabled, arrive, tick, waves: () => waves.size }
}
