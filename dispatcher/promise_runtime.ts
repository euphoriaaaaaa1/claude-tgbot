/**
 * 承诺兑现 · IO 层：.promises.json 的读写、60s tick 编排、到期注入、欠账载体。
 *
 * 决策全在 promise_plan.ts（纯函数），这里只做"读写文件 / 问作息桥 / 调注入回调"。
 * 依赖全部注入（时钟、probe、inject、ensureWorker、日志），所以能拿假时钟 + 临时目录白盒测。
 * 形状照抄 hang_runtime，但状态机独立——hang 是"入站即取消"，承诺是"到点必须发"，语义相反。
 *
 * 铁律：
 * - **tick 内零同步阻塞子进程调用**：作息 probe 与拉活都是异步的（dispatcher 是全部 bot
 *   的命脉，spawnSync 卡住 = 全线失联）。
 * - 先置 fired 并落盘、**再**写 inbox：顺序反了宁可漏发一次（重投兜住），绝不重发。
 * - 送达判据是会话 jsonl 里真出现了这条 user 行，**不是** inbox 文件还在不在。
 * - 日志只写 promise_id / chat_id / 状态 / 原因码，绝不写 gist 或任何正文。
 */
import { chmodSync, copyFileSync, existsSync, linkSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from 'fs'
import { basename, join } from 'path'
import { randomBytes } from 'crypto'
import { LAST_OUTBOUND_FILE, markOutbound, readLastOutbound, writeJsonAtomic, deliveryEvidence, type OutboundEntry } from './promise_wire'
import {
  ACTIVE_STATES, MAX_ATTEMPTS, MAX_PENDING_PER_CHAT, DEFER_GIVEUP_MS, RETRY_AFTER_MS, SPAWN_THROTTLE_MS,
  DEDUP_WINDOW_MS, emptyCounts, formatTickLine, formatWarn, hasAction, hasRecentInbound, awakeDecision,
  DONE_WINDOW_MS, WAKE_GRACE_MS, PROBE_FAIL_MAX, RECENT_INBOUND_MS, PROBE_FALLBACK_AWAKE, inSleepWindow, localHHMM, relativeStamp,
  OWED_INFLIGHT_RESTORE_MS, OWED_REASONS, chatSceneOf, expiredOwedIds, gistDedupKey, inPromiseScope, lastHumanInbound, isPurgeable, isUsableOwed, isUsableRecord, localStamp, makeOwedEntry, parseDueAt, pruneOwed, renderDueText, renderOwedText,
  validateGist, type OwedEntry, type OwedReason, type PromiseRecord, type PromiseStore, type TickCounts,
  type WarnToken, type ProbeOutcome, type LateReason,
} from './promise_plan'

export { LAST_OUTBOUND_FILE, markOutbound, writeJsonAtomic }

export const STORE_FILE = '.promises.json'
export const OWED_FILE = '.promise-owed.json'
const LOCK_FILE = '.promises.lock'
const LAST_INBOUND_FILE = '.last-inbound-ts.json'
/** 持锁只包一段同步读-改-写，超过这个年龄的锁一定是进程被杀留下的残骸 */
const LOCK_STALE_MS = 15_000
const WARN_THROTTLE_MS = 10 * 60_000

export type PromiseInject = (chatId: string, key: string, meta: Record<string, unknown>) => void

export type PromiseDeps = {
  /** bot 的通道目录：inbox/ 与 .promises.json 都在这里 */
  channelDir: string
  inject: PromiseInject
  /** 会话 jsonl 当前字节数；取不到返回 -1。发出时记下，确认时从这里往后扫 */
  sessionSize?: () => number
  /** 从字节偏移起逐行扫描会话 jsonl 到文件末尾（偏移失效 → 从头扫）；取不到就不回调（→ 判未送达，绝不误判成已送达） */
  scanSession: (fromOffset: number, onLine: (line: string) => void) => void
  /** 作息探测四值；不传 atMs 探当下（tick 用），传 atMs 探该时刻（登记告知用）；抛错/reject 按 unknown */
  probeAsleep: (atMs?: number) => Promise<ProbeOutcome>
  /** 撤掉上一件还没被消费的 inbox 文件（重投前调用）；不传 = 不撤 */
  retract?: (chatId: string, key: string) => void
  /** 异步拉活 worker；不传 = 不拉活（测试/纯注入场景） */
  ensureWorker?: (chatId: string) => void
  /** 停机闸：返回非 null 即顺延（不丢弃、不改 due_at） */
  isPaused?: () => string | null
  /** 功能级关停开关：true 时 tick 整体不动作，已登记数据不丢 */
  isStopped?: () => boolean
  now?: () => number
  log?: (line: string) => void
  /** 写请求等锁的上限（毫秒），默认 3000；等不到返回 store_failed */
  lockWaitMs?: number
}

export type PromiseHttpResult = { status: number; body: Record<string, unknown> }
export type PromiseRuntime = {
  handle: (body: unknown) => Promise<PromiseHttpResult>
  tick: () => Promise<void>
}

/* ── 小工具（writeJsonAtomic 在 promise_wire.ts：带 fsync 的原子写，与出站表共用）── */
function readJsonObject(path: string): Record<string, any> {
  try {
    const o = JSON.parse(readFileSync(path, 'utf8'))
    return o && typeof o === 'object' && !Array.isArray(o) ? o : {}
  } catch { return {} }
}

/**
 * 读一个 JSON 对象文件。损坏（非法 JSON / 不是对象 / 空文件）时把原文件原样挪成
 * `<file>.corrupt-<ms>-<rand>` 备份、按空表重建（R2）。只能在持锁时调用：不持锁挪文件，
 * 可能把别人刚原子写好的新表挪走。返回 null = 损坏但备份失败 → 调用方绝不能写（写就是无备份覆盖）。
 */
function loadJsonForWrite(path: string, nowMs: number, onCorrupt: (note: string) => void): Record<string, any> | null {
  let reason = ''
  try {
    const o = JSON.parse(readFileSync(path, 'utf8'))
    if (o && typeof o === 'object' && !Array.isArray(o)) return o
    reason = 'not_object'
  } catch (e) {
    if ((e as NodeJS.ErrnoException)?.code === 'ENOENT') return {}
    reason = 'parse_failed'
  }
  const bak = `${path}.corrupt-${nowMs}-${randomBytes(3).toString('hex')}`
  try { renameSync(path, bak) } catch {
    onCorrupt(`file=${basename(path)} reason=${reason} backup=failed`)
    return null
  }
  try { writeJsonAtomic(path, {}) } catch {}
  onCorrupt(`file=${basename(path)} reason=${reason} backup=${basename(bak)}`)
  return {}
}

/* ── 欠账载体（契约 §6.1 / §6.2）────────────────────────────────────── */

/**
 * 定位欠账文件：参数既可能是 bot 通道目录本身，也可能是放着多个 bot 的通道根目录。
 * 按文件存在性判定，调用方不必知道目录分几层。找不到就返回空串（上层按"无欠账"处理）。
 */
function resolveOwedPath(channelDir: string): string {
  if (typeof channelDir !== 'string' || !channelDir) return ''
  const direct = join(channelDir, OWED_FILE)
  if (existsSync(direct)) return direct
  // 这里就是 bot 通道目录（有 inbox/）→ 没文件就是真没欠账，别再往下扫目录：
  // 这个函数挂在每条消息的投递路径上，常态必须是两次 existsSync 就返回。
  if (existsSync(join(channelDir, 'inbox'))) return direct
  const bot = process.env.BOT_NAME
  if (bot && existsSync(join(channelDir, bot, OWED_FILE))) return join(channelDir, bot, OWED_FILE)
  // 通道根下恰好一个 bot 有欠账文件时用它；多于一个说明调用方给错了目录，宁可不猜
  try {
    const hits = readdirSync(channelDir, { withFileTypes: true })
      .filter(d => d.isDirectory() && existsSync(join(channelDir, d.name, OWED_FILE)))
      .map(d => join(channelDir, d.name, OWED_FILE))
    if (hits.length === 1) return hits[0]
  } catch {}
  return direct
}

/** 在途欠账：已拼进某条投递、还没确认送达的条目（R6）。与欠账文件同目录，同一把锁 */
export const OWED_INFLIGHT_FILE = '.promise-owed-inflight.json'
type InflightEntry = OwedEntry & { taken_at: number }
const isInflight = (e: unknown): e is InflightEntry => isUsableOwed(e) && Number.isFinite((e as InflightEntry).taken_at)
function inflightMap(doc: Record<string, any>): Record<string, unknown[]> {
  const m = doc.inflight
  return m && typeof m === 'object' && !Array.isArray(m) ? m : {}
}
const inflightOf = (doc: Record<string, any>, chatId: string): InflightEntry[] => {
  const list = inflightMap(doc)[chatId]
  return Array.isArray(list) ? list.filter(isInflight) : []
}
const owedDirOf = (owedPath: string) => owedPath.slice(0, owedPath.length - OWED_FILE.length)
/** worker 侧（取欠账/确认欠账）的告警出口：它没有 dispatcher 的 log，直接写 stderr（N4：坏文件备份了也要让人看得到） */
const stderrLine = (line: string) => { try { process.stderr.write(`${line}\n`) } catch {} }
/** 在途文件里还没被 tick 处理的认账回执对应的承诺 */
const settledIdsOf = (doc: Record<string, any>): Set<string> =>
  new Set((Array.isArray(doc.settled) ? doc.settled : []).map((r: any) => String(r?.promise_id)))

/**
 * 取出该 chat 的欠账，返回渲染好的认账文本（无欠账 → 空串）。由 worker-plugin 投递下一条消息时调用。
 * 取走的条目从 .promise-owed.json 删键（契约 §6.2），但先挪进在途文件：确认送达前不删（R6），
 * 超过 OWED_INFLIGHT_RESTORE_MS 仍未确认的，下一次取时连同新欠账一起再带上——宁可多认一次，不可丢一件。
 * 任何异常都返回空串——绝不阻断投递。
 */
export function takePromiseOwed(channelDir: string, chatId: string, nowMs = Date.now(), warnLine = stderrLine): string {
  try {
    const path = resolveOwedPath(channelDir)
    if (!path) return ''
    const flPath = join(owedDirOf(path), OWED_INFLIGHT_FILE)
    const key = String(chatId)
    const stale = (doc: Record<string, any>) => inflightOf(doc, key).filter(e => nowMs - e.taken_at > OWED_INFLIGHT_RESTORE_MS)
    // 热路径先无锁看一眼：本 chat 既无欠账也无该恢复的在途就不碰锁（S6）。真正取走在锁内重读
    if (!Array.isArray(readJsonObject(path)[key]) && !stale(readJsonObject(flPath)).length) return ''
    const lock = join(owedDirOf(path), LOCK_FILE)
    const token = acquireLock(lock)
    if (!token) return ''                               // 抢不到锁就不取：欠账留给下一条消息，零损失（R1）
    try {
      const onCorrupt = (note: string) => warnLine(formatWarn('STORE-CORRUPT', note))
      const doc = loadJsonForWrite(path, nowMs, onCorrupt)
      const fl = loadJsonForWrite(flPath, nowMs, onCorrupt)
      if (!doc || !fl) return ''
      const restore = stale(fl)
      const byId = new Map<string, OwedEntry>()
      for (const e of [...restore, ...(Array.isArray(doc[key]) ? doc[key] : [])]) {
        if (isUsableOwed(e)) byId.set(e.promise_id, { promise_id: e.promise_id, due_at: e.due_at,
          due_at_local: e.due_at_local, gist: e.gist, owed_since: e.owed_since, reason: e.reason })
      }
      const { kept } = pruneOwed([...byId.values()], nowMs)
      // 有该恢复的在途条目时，即使它们全都过了 7 天也要走下面的写回把它们清掉（N1）：否则它们永远留在
      // 在途文件里，这个聊天之后每条消息都会因"有该恢复的"去抢一次锁
      if (!kept.length && !restore.length && !Array.isArray(doc[key])) return ''
      // 先写在途再删欠账：两步之间崩了，顶多下次多认一次，不会丢
      const map = inflightMap(fl)
      const keep = inflightOf(fl, key).filter(e => !restore.includes(e))
      const next = [...keep, ...kept.map(e => ({ ...e, taken_at: nowMs }))]
      if (next.length) map[key] = next; else delete map[key]
      fl.inflight = map
      writeJsonAtomic(flPath, fl)
      delete doc[key]                                   // 无条目的 chat 不留空数组，整个键删掉
      writeJsonAtomic(path, doc)
      return kept.length ? renderOwedText(kept) : ''
    } finally { releaseLock(lock, token) }
  } catch { return '' }
}

/**
 * worker 确认"带着欠账的那条投递"已进会话后调用：在途里 taken_at ≤ takenAtMs 的条目了结。
 * 返回 false = 这次没办成（拿不到锁/读写失败），条目仍在途，超时后会被恢复重带，不会丢。
 */
export function confirmPromiseOwed(
  channelDir: string, chatId: string, takenAtMs: number, nowMs = Date.now(), warnLine = stderrLine,
): boolean {
  try {
    const path = resolveOwedPath(channelDir)
    if (!path) return false
    const flPath = join(owedDirOf(path), OWED_INFLIGHT_FILE)
    const key = String(chatId)
    if (!inflightOf(readJsonObject(flPath), key).length) return true
    const lock = join(owedDirOf(path), LOCK_FILE)
    const token = acquireLock(lock)
    if (!token) return false
    try {
      const fl = loadJsonForWrite(flPath, nowMs, note => warnLine(formatWarn('STORE-CORRUPT', note)))
      if (!fl) return false
      const map = inflightMap(fl)
      const list = inflightOf(fl, key)
      const rest = list.filter(e => e.taken_at > takenAtMs)
      if (rest.length === list.length) return true
      if (rest.length) map[key] = rest; else delete map[key]
      fl.inflight = map
      // 认账回执：dispatcher tick 读到后在锁内把这些承诺置终态 settled（R7；worker 不直接改账本）
      const receipts = Array.isArray(fl.settled) ? fl.settled : []
      fl.settled = [...receipts, ...list.filter(e => e.taken_at <= takenAtMs)
        .map(e => ({ promise_id: e.promise_id, chat_id: key, at: nowMs, reason: e.reason }))]   // reason 供无出站时按原因重挂
      writeJsonAtomic(flPath, fl)
      return true
    } finally { releaseLock(lock, token) }
  } catch { return false }
}

/* ── 锁：所有读-改-写必须持锁后进行（R1）。锁文件内容是持有者的唯一 token ──
 * 拿不到锁的一方一律不写：原子写只保证文件不半截，防不了读-改-写丢更新。
 * 过期锁（持有者崩了）用 rename 原子接管：同一把旧锁只有一方挪得走；挪走的若不是过期锁
 * （别人刚建的新锁）就原样放回并让开（S7、N2）。时刻一律用真实时钟，
 * 锁文件 mtime 是真实时间，不能拿调用方注入的"现在"比。 */
function acquireLock(lockPath: string): string | null {
  const token = `${process.pid}.${randomBytes(6).toString('hex')}`
  try { writeFileSync(lockPath, token, { flag: 'wx', mode: 0o600 }); return token } catch {}
  return takeoverStaleLock(lockPath, token) ? token : null
}

/**
 * 接管过期锁；成功返回 true（锁已写成 token）。afterStaleCheck 只供单测在"判过期"与"rename"之间
 * 插入别人抢先建好新锁的时序（N2），生产不传。
 */
export function takeoverStaleLock(lockPath: string, token: string, afterStaleCheck: () => void = () => {}): boolean {
  try {
    if (Date.now() - statSync(lockPath).mtimeMs <= LOCK_STALE_MS) return false
    afterStaleCheck()
    const moved = `${lockPath}.stale.${token}`
    renameSync(lockPath, moved)
    // 判据是"挪出来的这个文件本身仍然过期"（N2）：rename 不改 mtime，别人在我 stat 之后刚建的新锁一定判不过期，
    // 就原样放回并让开。不再比对内容——stat 与读内容是两次系统调用，中间被人整套接管过就会误删别人的新锁
    if (Date.now() - statSync(moved).mtimeMs <= LOCK_STALE_MS) {
      try { linkSync(moved, lockPath) } catch {}          // link 在目标已存在时失败，不会盖掉更新的锁
      rmSync(moved, { force: true })
      return false
    }
    rmSync(moved, { force: true })
    writeFileSync(lockPath, token, { flag: 'wx', mode: 0o600 })
    return true
  } catch { return false }
}
/** 只删自己的锁：持锁超时被别人接管后，不能把别人的锁删掉 */
function releaseLock(lockPath: string, token: string | null): void {
  if (!token) return
  try { if (readFileSync(lockPath, 'utf8') === token) rmSync(lockPath, { force: true }) } catch {}
}

export function createPromiseRuntime(deps: PromiseDeps): PromiseRuntime {
  const { channelDir, inject, scanSession, probeAsleep } = deps
  const sessionSize = deps.sessionSize ?? (() => -1)
  const now = deps.now ?? Date.now
  const log = deps.log ?? (() => {})
  const isPaused = deps.isPaused ?? (() => null)
  const isStopped = deps.isStopped ?? (() => false)
  const lockWaitMs = deps.lockWaitMs ?? 3000
  const storePath = join(channelDir, STORE_FILE)
  const storeBakPath = `${storePath}.bak`
  const storeAlertPath = join(channelDir, '.promises.alert')
  const owedPath = join(channelDir, OWED_FILE)
  const inflightPath = join(channelDir, OWED_INFLIGHT_FILE)
  const lockPath = join(channelDir, LOCK_FILE)
  const lastInboundPath = join(channelDir, LAST_INBOUND_FILE)

  const warnAt = new Map<WarnToken, number>()
  const spawnAt = new Map<string, number>()
  let ticking = false
  let lastIssuedMs = 0

  function warn(token: WarnToken, note = ''): void { log(formatWarn(token, note)) }
  /** 同一原因码 10 分钟只吼一次，免得坏了一整夜刷满日志 */
  function warnThrottled(token: WarnToken, note = ''): void {
    const t = now()
    if (t - (warnAt.get(token) ?? 0) < WARN_THROTTLE_MS) return
    warnAt.set(token, t)
    warn(token, note)
  }

  /** 每次都从盘上重读：盘上才是权威（多实例、重启、worker 侧取欠账都会改它） */
  function loadStore(): PromiseStore {
    // 不持锁的预读：损坏只当空表看（不误触发），挪备份留给持锁的一方做
    try {
      const o = JSON.parse(readFileSync(storePath, 'utf8'))
      return o && typeof o === 'object' && !Array.isArray(o) ? o as PromiseStore : {}
    } catch { return {} }
  }

  /** 本次持锁读到的账本是否完好；只有完好时 persistStore 才把它复制成 .bak（P8：.bak 永远是最后一份完好表） */
  let storeLoadedOk = true

  /**
   * 持锁时读账本（P8）：损坏 → 原文件挪成 .corrupt-* 备份；再试 `.bak`，能解析为对象就写回并
   * `STORE-CORRUPT … restored=bak`，否则空表 `restored=none`；同时写一行 `.promises.alert`
   * （`<ms> <file> <reason> <restored>`，0600）让人事后看得见。null = 损坏且备份失败，不得写。
   */
  function loadStoreLocked(): PromiseStore | null {
    storeLoadedOk = true
    let reason = ''
    try {
      const o = JSON.parse(readFileSync(storePath, 'utf8'))
      if (o && typeof o === 'object' && !Array.isArray(o)) return o as PromiseStore
      reason = 'not_object'
    } catch (e) {
      if ((e as NodeJS.ErrnoException)?.code === 'ENOENT') return {}
      reason = 'parse_failed'
    }
    storeLoadedOk = false
    const t = now()
    const corrupt = `${storePath}.corrupt-${t}-${randomBytes(3).toString('hex')}`
    try { renameSync(storePath, corrupt) } catch {
      warn('STORE-CORRUPT', `file=${basename(storePath)} reason=${reason} backup=failed`)
      return null
    }
    let restored: PromiseStore | null = null
    try {
      const b = JSON.parse(readFileSync(storeBakPath, 'utf8'))
      if (b && typeof b === 'object' && !Array.isArray(b)) restored = b as PromiseStore
    } catch { /* 无 .bak 或 .bak 也坏 → 空表 */ }
    const tag = restored ? 'bak' : 'none'
    try { writeJsonAtomic(storePath, restored ?? {}) } catch {}
    try { writeFileSync(storeAlertPath, `${t} ${basename(storePath)} ${reason} ${tag}\n`, { mode: 0o600 }) } catch {}
    warn('STORE-CORRUPT', `file=${basename(storePath)} reason=${reason} backup=${basename(corrupt)} restored=${tag}`)
    return restored ?? {}
  }

  /** 预读看到的是不是一份坏表（不是对象 / 解析失败）；文件不存在不算 */
  function storeLooksCorrupt(): boolean {
    try {
      const o = JSON.parse(readFileSync(storePath, 'utf8'))
      return !o || typeof o !== 'object' || Array.isArray(o)
    } catch (e) { return (e as NodeJS.ErrnoException)?.code !== 'ENOENT' }
  }

  function persistStore(store: PromiseStore): boolean {
    try {
      mkdirSync(channelDir, { recursive: true })
      // 写前把当前完好表留作 .bak；本次是从坏表恢复的那次写回则跳过，保证 .bak 永远是最后一份完好表
      if (storeLoadedOk && existsSync(storePath)) {
        try { copyFileSync(storePath, storeBakPath); chmodSync(storeBakPath, 0o600) } catch {}
      }
      writeJsonAtomic(storePath, store)
      return true
    } catch (e) {
      warn('STORE-WRITE-FAILED', `op=store reason=${e instanceof Error ? e.name : 'unknown'}`)
      return false
    }
  }

  function loadLastInbound(): Record<string, unknown> {
    return existsSync(lastInboundPath) ? readJsonObject(lastInboundPath) : {}
  }

  /** message_id 必须是纯数字串且不重复：同一轮里重投与首投可能撞同一毫秒 */
  function nextMessageMs(t: number): number {
    const ms = Math.max(t, lastIssuedMs + 1)
    lastIssuedMs = ms
    return ms
  }

  type InjectJob = { chatId: string; key: string; retractKey: string; meta: Record<string, unknown> }

  /**
   * 置 fired 并产出注入任务（真正写 inbox 在落盘成功之后）。lateReason 非 null 时正文带迟到原因段；
   * fire 后顺延三字段一并清 null（接口 A）。meta 带 promise_id 供 worker 按轮绑定出站（P3）。
   */
  function armFire(id: string, rec: PromiseRecord, t: number, lateReason: LateReason = null): InjectJob {
    const ms = nextMessageMs(t)
    const key = `promise${ms}`
    // 重投前把上一件撤掉：worker 长时间没起来时两件会一起被 drain 出来，
    // 用户就看到两条一模一样的兑现消息（I1）。已被 worker 读进内存的件撤不到，由它那侧按 promise_id 去重。
    const retractKey = typeof rec.inbox_key === 'string' ? rec.inbox_key : ''
    rec.state = 'fired'
    rec.fired_at = t
    rec.message_id = String(ms)
    rec.inbox_key = key
    rec.deferred_since = null
    rec.deferred_reason = null
    rec.defer_until = null
    rec.delivered_at = null
    const size = sessionSize()
    rec.jsonl_offset = Number.isInteger(size) && size >= 0 ? size : null   // 送达行只可能写在这之后
    const scene = chatSceneOf(rec.chat_id)
    // schema 与 hang / self-initiate 那条对齐（from_id 是 string、message_id 是数字串、
    // ts 是 ISO 8601，差一样 claude 就静默丢 notification）。'[' 开头 → burst 不合并。
    return {
      chatId: rec.chat_id,
      key,
      retractKey,
      meta: {
        text: renderDueText(rec.gist, Math.max(0, t - rec.due_at), lateReason, localStamp(rec.due_at)),
        promise_id: id,
        chat_id: rec.chat_id,
        // 聊天类型跟随送达聊天（R9）：群里答应的按群聊注入——不打【私聊】、不带私聊关系块
        scene,
        from_id: scene === 'group' && typeof rec.requester_id === 'string' && rec.requester_id ? rec.requester_id : rec.chat_id,
        chat_type: scene,
        is_bot_sender: false,
        ts: new Date(ms).toISOString(),
        message_id: String(ms),
      },
    }
  }

  /** 把欠账条目并进 .promise-owed.json（整理 + 限容）。写不进去返回 false，调用方据此不改状态。 */
  function appendOwed(adds: { chatId: string; entry: OwedEntry }[], t: number): boolean {
    try {
      const doc = loadJsonForWrite(owedPath, t, note => warn('STORE-CORRUPT', note))
      if (!doc) return false                          // 坏欠账文件备份不了：不覆盖，记录留在原状态下轮再来
      const fl = existsSync(inflightPath) ? readJsonObject(inflightPath) : {}
      const touched = new Set(adds.map(a => a.chatId))
      for (const chatId of touched) {
        const existing = Array.isArray(doc[chatId]) ? doc[chatId] : []
        // 同一承诺不重复记欠账（R7/S2）：上一轮欠账已落盘、账本没落盘时，下轮会再追加一次
        const seen = new Set<string>([...existing, ...inflightOf(fl, chatId)].map((e: any) => e?.promise_id))
        const fresh = adds.filter(a => a.chatId === chatId && !seen.has(a.entry.promise_id)).map(a => a.entry)
        const merged = [...existing, ...fresh]
        const { kept, overflowed } = pruneOwed(merged, t)
        if (overflowed > 0) warn('OWED-OVERFLOW', `chat=${chatId} dropped=${overflowed}`)
        if (kept.length) doc[chatId] = kept
        else delete doc[chatId]
      }
      mkdirSync(channelDir, { recursive: true })
      writeJsonAtomic(owedPath, doc)
      return true
    } catch (e) {
      warn('STORE-WRITE-FAILED', `op=owed reason=${e instanceof Error ? e.name : 'unknown'}`)
      return false
    }
  }

  /** 取消/终结时把欠账里的同一条一并清掉（含已拼进投递、尚未确认的在途条目），免得已作废的承诺还被端到她面前 */
  function dropOwed(chatId: string, promiseId: string, t: number): void {
    try {
      if (existsSync(owedPath)) {
        const doc = loadJsonForWrite(owedPath, t, note => warn('STORE-CORRUPT', note))
        if (doc && Array.isArray(doc[chatId])) {
          const { kept } = pruneOwed(doc[chatId].filter((e: any) => e?.promise_id !== promiseId), t)
          if (kept.length) doc[chatId] = kept
          else delete doc[chatId]
          writeJsonAtomic(owedPath, doc)
        }
      }
      if (existsSync(inflightPath)) {
        const fl = loadJsonForWrite(inflightPath, t, note => warn('STORE-CORRUPT', note))
        const list = fl ? inflightOf(fl, chatId) : []
        const rest = list.filter(e => e.promise_id !== promiseId)
        if (fl && rest.length !== list.length) {
          const map = inflightMap(fl)
          if (rest.length) map[chatId] = rest; else delete map[chatId]
          fl.inflight = map
          writeJsonAtomic(inflightPath, fl)
        }
      }
    } catch { /* 清不掉不影响取消本身 */ }
  }

  function throttledEnsureWorker(chatId: string, t: number): void {
    if (!deps.ensureWorker) return
    const last = spawnAt.get(chatId) ?? 0
    if (t - last < SPAWN_THROTTLE_MS) { warnThrottled('SPAWN-THROTTLED', `chat=${chatId}`); return }
    spawnAt.set(chatId, t)
    try { deps.ensureWorker(chatId) } catch { /* 拉活失败不影响注入：worker 起来会 drain inbox */ }
  }

  /* ── HTTP 端点（/promise）───────────────────────────────────────── */
  async function handle(rawBody: unknown): Promise<PromiseHttpResult> {
    const body = (rawBody && typeof rawBody === 'object' ? rawBody : {}) as Record<string, any>
    const action = body.action
    if (action !== 'set' && action !== 'cancel' && action !== 'list') {
      return { status: 400, body: { ok: false, error: 'bad_action' } }
    }
    const t = now()
    const token = await acquireLockAsync()
    // 写操作拿不到锁就不写（R1）：返回 store_failed，调用方明确知道"没记下"，绝不无锁读-改-写。
    // list 只读：原子写保证读到的永远是一份完整的表，拿不到锁照样可以读。
    if (!token && action !== 'list') {
      warn('STORE-WRITE-FAILED', 'op=lock reason=busy')
      return { status: 500, body: { ok: false, error: 'store_failed' } }
    }
    let result: PromiseHttpResult
    try {
      // ── 以下到 persist 之间不得出现 await：读-改-写不许被另一条请求插进来 ──
      if (action === 'list') return { status: 200, body: listPromises((token ? loadStoreLocked() : loadStore()) ?? {}, body) }
      const store = loadStoreLocked()
      if (!store) return { status: 500, body: { ok: false, error: 'store_failed' } }   // 坏表备份不了：宁可不记，也不无备份覆盖
      if (action === 'cancel') return cancelPromise(store, body, t)
      result = setPromise(store, body, t)
    } finally { releaseLock(lockPath, token) }
    // 登记告知（P2，锁已释放再探，不拖慢别的写请求）：due_at 落在睡眠时段 → due_in_sleep:true；其余一律不提示
    return annotateDueInSleep(result, t)
  }

  /**
   * probeAsleep(due_at) 返回 asleep 才加 due_in_sleep；awake/unknown/unsupported/超时/抛错 → 不出现；deduped 不探测。
   * due 落在 RECENT_INBOUND_MS 内也不探测：登记发生在真人刚说话的那一轮，到期时 runtime 按真人入站特例免探测照发，探了也白探。
   */
  async function annotateDueInSleep(r: PromiseHttpResult, t: number): Promise<PromiseHttpResult> {
    const b = r.body
    if (b.ok !== true || b.deduped === true || typeof b.due_at !== 'number' || b.due_at - t < RECENT_INBOUND_MS) return r
    let probe: ProbeOutcome = 'unknown'
    try { probe = await probeAsleep(b.due_at as number) } catch { probe = 'unknown' }
    return probe === 'asleep' ? { status: r.status, body: { ...b, due_in_sleep: true } } : r
  }

  /** 等锁最多 lockWaitMs（默认 3 秒）。持锁段全是同步的毫秒级读写，等不到说明锁被卡住了 */
  async function acquireLockAsync(): Promise<string | null> {
    const deadline = Date.now() + lockWaitMs
    for (;;) {
      const token = acquireLock(lockPath)
      if (token || Date.now() >= deadline) return token
      await new Promise(r => setTimeout(r, 20))
    }
  }

  function listPromises(store: PromiseStore, body: Record<string, any>): Record<string, unknown> {
    const chatId = body.chat_id != null && String(body.chat_id) !== '' ? String(body.chat_id) : null
    // 插件传 scope_chat_id（当前聊天）时只列来源或送达聊天等于它的（§9-1）；HTTP 直连不传，行为照旧
    const scope = typeof body.scope_chat_id === 'string' && body.scope_chat_id ? body.scope_chat_id : null
    if ('scope_chat_id' in body && !scope) return { ok: false, error: 'chat_not_allowed' }   // 传了却无效：一条都不列（§11-2）
    const promises = Object.entries(store)
      .filter(([, r]) => isUsableRecord(r) && ACTIVE_STATES.includes(r.state) && (!chatId || r.chat_id === chatId)
        && (!scope || inPromiseScope(r, scope)))
      .sort((a, b) => a[1].due_at - b[1].due_at)
      .map(([id, r]) => ({ promise_id: id, due_at: r.due_at, gist: r.gist, state: r.state }))
    return { ok: true, promises }
  }

  function cancelPromise(store: PromiseStore, body: Record<string, any>, t: number): PromiseHttpResult {
    const id = typeof body.promise_id === 'string' ? body.promise_id.trim() : ''
    if (!id) return { status: 200, body: { ok: false, error: 'missing_promise_id' } }   // 没有"取消最近一条"的语义
    const rec = store[id]
    // 插件传 scope_chat_id（当前聊天）时先判范围再判存在（§11-2）：作用聊天无效，或记录存在但来源与送达
    // 聊天都不是它，不论状态一律 chat_not_allowed；范围内的才轮到 not_found。HTTP 直连不传，行为照旧
    if ('scope_chat_id' in body) {
      const scope = typeof body.scope_chat_id === 'string' ? body.scope_chat_id : ''
      if (!scope || (isUsableRecord(rec) && !inPromiseScope(rec, scope))) {
        return { status: 200, body: { ok: false, error: 'chat_not_allowed' } }
      }
    }
    if (!isUsableRecord(rec) || !ACTIVE_STATES.includes(rec.state)) return { status: 200, body: { ok: false, error: 'not_found' } }
    const wasOwed = rec.state === 'owed'
    const firedKey = rec.state === 'fired' && typeof rec.inbox_key === 'string' ? rec.inbox_key : ''
    rec.state = 'cancelled'
    rec.closed_at = t
    if (!persistStore(store)) return { status: 500, body: { ok: false, error: 'store_failed' } }
    if (wasOwed) dropOwed(rec.chat_id, id, t)        // 已作废的承诺别再端到她面前
    // 已写进 inbox、worker 还没取走的到点件一并撤掉（S1）：她这轮刚说"算了"，下一轮就别再端上来
    if (firedKey && deps.retract) try { deps.retract(rec.chat_id, firedKey) } catch { /* 已被取走则撤不到 */ }
    log(`promise_cancel id=${id} chat=${rec.chat_id}`)
    return { status: 200, body: { ok: true, cancelled: id } }
  }

  function setPromise(store: PromiseStore, body: Record<string, any>, t: number): PromiseHttpResult {
    const g = validateGist(body.gist)
    if (!g.ok) {
      return { status: 200, body: g.error === 'gist_too_long' ? { ok: false, error: g.error, len: g.len } : { ok: false, error: g.error } }
    }
    const d = parseDueAt(body.due_at, t)
    if (!d.ok) {
      return { status: 200, body: d.error === 'bad_due_at' ? { ok: false, error: d.error, detail: d.detail } : { ok: false, error: d.error } }
    }
    const chatId = body.chat_id != null ? String(body.chat_id).trim() : ''
    if (!chatId) return { status: 200, body: { ok: false, error: 'missing_chat_id' } }

    // 幂等：同一 chat、due_at 相差 <60s、且 gist 规范化后相同，才视为同一条（模型嘴瓢说两遍），返回已存在的 id。
    // gist 不同的是同一分钟里的两件事，必须各自登记（契约 §7 r2），否则第二件会被悄悄吞掉。
    // 规范化为空串（纯表情/标点）时按原文比较（r3），见 gistDedupKey。
    const gistKey = gistDedupKey(g.gist)
    for (const [id, r] of Object.entries(store)) {
      if (!isUsableRecord(r) || r.state !== 'pending' || r.chat_id !== chatId) continue
      if (Math.abs(r.due_at - d.dueAt) < DEDUP_WINDOW_MS && gistDedupKey(r.gist) === gistKey) {
        return { status: 200, body: { ok: true, promise_id: id, due_at: r.due_at, due_at_local: localStamp(r.due_at), due_in: relativeStamp(r.due_at, t), deduped: true } }
      }
    }
    const pending = Object.values(store).filter(r => isUsableRecord(r) && r.state === 'pending' && r.chat_id === chatId).length
    if (pending >= MAX_PENDING_PER_CHAT) {
      return { status: 200, body: { ok: false, error: 'too_many_pending', count: pending } }
    }
    const promiseId = `p-${t.toString(36)}-${randomBytes(4).toString('hex')}`
    const requesterId = typeof body.requester_id === 'string' && body.requester_id.trim() ? body.requester_id.trim() : null
    const sourceChatId = typeof body.source_chat_id === 'string' && body.source_chat_id.trim() ? body.source_chat_id.trim() : chatId
    store[promiseId] = {
      chat_id: chatId, due_at: d.dueAt, gist: g.gist, created_at: t, requester_id: requesterId, source_chat_id: sourceChatId,
      state: 'pending', attempts: 0, fired_at: null, message_id: null, inbox_key: null, deferred_since: null,
      deferred_reason: null, defer_until: null, probe_fails: 0, delivered_at: null,
    }
    if (!persistStore(store)) {
      delete store[promiseId]                       // 没落盘就当没登记：绝不返回 ok:true
      return { status: 500, body: { ok: false, error: 'store_failed' } }
    }
    if (d.clamped) warn('CLAMPED', `id=${promiseId} chat=${chatId}`)
    log(`promise_set id=${promiseId} chat=${chatId} due=${d.dueAt}${d.clamped ? ' clamped=1' : ''}`)
    return {
      status: 200,
      body: { ok: true, promise_id: promiseId, due_at: d.dueAt, due_at_local: localStamp(d.dueAt), due_in: relativeStamp(d.dueAt, t), ...(d.clamped ? { clamped: true } : {}) },
    }
  }

  /* ── 60s tick ───────────────────────────────────────────────────── */
  async function tick(): Promise<void> {
    if (ticking) return                               // tick 变异步后必须自锁，否则慢 probe 会叠轮
    ticking = true
    try { await runTick() } finally { ticking = false }
  }

  async function runTick(): Promise<void> {
    const t = now()
    // 功能开关（I6）：到期的一律不动（留在 pending，关掉开关后照常补发）；但已发出、到了重投时刻的
    // 不再重投，确认没送达就转欠账 paused（§8-2 裁定：停用/暂停/功能开关都算闸，不受 24 小时限制）
    const stopped = isStopped()
    if (stopped) warnThrottled('STOPPED', `bot_switch`)

    // 坏表：本轮只做"备份 + 空表重建 + 告警"，不触发任何承诺（E7）。挪文件必须持锁
    if (storeLooksCorrupt()) {
      const token = acquireLock(lockPath)
      if (token) try { loadStoreLocked() } finally { releaseLock(lockPath, token) }
      return
    }
    // 先看盘上有没有事可做：没有就零 IO、零输出（常态是没有）
    const preload = loadStore()
    let pendingDue = 0
    let firedCount = 0
    let purgeable = 0
    let retryDue = 0
    for (const r of Object.values(preload)) {
      if (!isUsableRecord(r)) continue
      if (r.state === 'pending' && t >= r.due_at) pendingDue++
      else if (r.state === 'fired') {
        firedCount++
        if (Number.isFinite(r.fired_at) && t - Number(r.fired_at) >= RETRY_AFTER_MS) retryDue++
      } else if (r.state === 'delivered') {
        firedCount++
        if (Number.isFinite(r.delivered_at) && t - Number(r.delivered_at) >= DONE_WINDOW_MS) retryDue++
      } else if (!stopped && isPurgeable(r, t)) purgeable++
    }
    const flDoc = existsSync(inflightPath) ? readJsonObject(inflightPath) : {}
    const receiptsDue = !stopped && Array.isArray(flDoc.settled) && flDoc.settled.length > 0
    // 欠账已经没了的 owed（§9-2）：不持锁先粗看一眼，有候选才进锁复核，免得长期欠着的账每分钟都抢锁
    const expiryDue = !stopped && expiredOwedIds(preload, existsSync(owedPath) ? readJsonObject(owedPath) : {},
      inflightMap(flDoc), settledIdsOf(flDoc), t).length > 0
    if (pendingDue === 0 && firedCount === 0 && purgeable === 0 && !receiptsDue && !expiryDue) return
    if (stopped && retryDue === 0 && pendingDue === 0) return   // 开关打开：只给到期/到重投时刻的记顺延，其余一概不动

    // 到期与重投走同一套闸（R4）：暂停/判睡对重投同样生效，否则半夜或 pause 期间照样注入并拉 worker
    const mayDeliver = pendingDue > 0 || retryDue > 0
    const paused = stopped ? 'feature_stopped' : mayDeliver ? isPaused() : null
    if (paused) warnThrottled('PAUSED', `gate=${paused}`)

    // ── 异步侧：作息 probe 与会话尾读都在持锁之前做完，锁内一律同步 ──
    let probe: ProbeOutcome = 'unknown'
    if (mayDeliver && !paused) {
      try { probe = await probeAsleep() } catch { probe = 'unknown' }
      if (probe === 'unknown') warnThrottled('PROBE-FAILED', `due=${pendingDue + retryDue}`)
      if (probe === 'unsupported') warnThrottled('PROBE-UNSUPPORTED', `bot=${basename(channelDir)}`)
    }
    const delivered = firedCount > 0 ? findDelivered(preload) : new Set<string>()
    const inbound = mayDeliver ? loadLastInbound() : {}
    const outbound = firedCount > 0 || receiptsDue ? readLastOutbound(channelDir) : {}
    const recentInbound = (chatId: string) => hasRecentInbound(lastHumanInbound(inbound, chatId), t)
    // 探测长期坏（probe_fails≥PROBE_FAIL_MAX）或无配置（unsupported）时的兜底：固定窗口 [09:00,22:00) 内即醒
    const fallbackAwake = inSleepWindow(localHHMM(t), [PROBE_FALLBACK_AWAKE])
    /** 一条记录此刻的醒睡判定（纯探测口径 + 兜底窗口）；并维护 probe_fails。真人入站特例由调用方先判 */
    const decideFor = (rec: PromiseRecord): 'awake' | 'asleep' | 'unknown' => {
      const fails = Number.isFinite(rec.probe_fails) ? Number(rec.probe_fails) : 0
      if (probe === 'awake' || probe === 'asleep') { rec.probe_fails = 0; return probe }
      if (probe === 'unsupported') return awakeDecision(probe, fallbackAwake)
      rec.probe_fails = fails + 1
      return awakeDecision('unknown', rec.probe_fails >= PROBE_FAIL_MAX ? fallbackAwake : null)
    }
    /** 绑定本件的出站：lastOutbound[chat].promise_ids ∋ id 且 ms ≥ sinceMs */
    const boundOutbound = (id: string, rec: PromiseRecord, sinceMs: number): OutboundEntry | null => {
      const o = outbound[rec.chat_id]
      return o && o.promise_ids.includes(id) && o.ms >= sinceMs ? o : null
    }

    const tickToken = acquireLock(lockPath)
    if (!tickToken) return                            // 另一个实例正在处理这一轮，安静让开
    const counts = emptyCounts()
    let jobs: InjectJob[] = []
    let rateLimited = 0
    try {
      const store = loadStoreLocked()                 // 锁内重读，拿最新的盘上状态
      if (!store) return                              // 坏表且备份不了：本轮一个字都不写
      const owedAdds: { chatId: string; entry: OwedEntry }[] = []
      const owedApply: { id: string; kind: 'owed' | 'failed' | 'silent'; retractKey?: string }[] = []
      const retracts: { chatId: string; key: string }[] = []
      let changed = false

      /** 记为顺延中（首次置 deferred_since），缓冲作废；累计超上限 → 转 owed(deferred_reason)（P2/P6） */
      const defer = (id: string, rec: PromiseRecord, reason: 'asleep' | 'paused', stage: string): void => {
        const since = Number.isFinite(rec.deferred_since) && Number(rec.deferred_since) > 0 ? Number(rec.deferred_since) : t
        rec.deferred_since = since; rec.deferred_reason = reason; rec.defer_until = null; changed = true
        // 顺延累计从到点算起（deferred_since 只记首次被挡的时刻）：进程停了几天再回来，不再多等 7 天
        if (t - Math.min(since, rec.due_at) > DEFER_GIVEUP_MS) {
          owedAdds.push({ chatId: rec.chat_id, entry: makeOwedEntry(id, rec, reason, t) })
          owedApply.push({ id, kind: 'owed', retractKey: typeof rec.inbox_key === 'string' ? rec.inbox_key : '' })
          return
        }
        counts.deferred++
        log(`promise_deferred id=${id} chat=${rec.chat_id} reason=${reason}${stage}${probe === 'unknown' ? ' fallback=default_window' : ''}`)
      }
      /** 闸开 / 判醒：曾顺延过的记录先走 WAKE_GRACE_MS 起床缓冲（真人入站免）；返回 null = 本轮还不能发 */
      const graceOrLate = (rec: PromiseRecord, inboundNow: boolean, justDeferred: boolean): { late: LateReason } | null => {
        const wasDeferred = Number.isFinite(rec.deferred_since) && Number(rec.deferred_since) > 0
        if (!wasDeferred && !justDeferred) return { late: null }
        if (!wasDeferred) { rec.deferred_since = t; rec.deferred_reason = 'asleep'; changed = true }
        if (!inboundNow) {
          const until = Number.isFinite(rec.defer_until) ? Number(rec.defer_until) : 0
          if (!until) { rec.defer_until = t + WAKE_GRACE_MS; changed = true; counts.deferred++; return null }
          if (t < until) { counts.deferred++; return null }
        }
        return { late: rec.deferred_reason ?? null }
      }
      /** 一条记录此刻能不能发：暂停 → paused；真人入站 → awake；否则探测口径（维护 probe_fails，始终落盘） */
      const gateFor = (rec: PromiseRecord): 'awake' | 'asleep' | 'unknown' | 'paused' => {
        if (paused) return 'paused'
        if (recentInbound(rec.chat_id)) return 'awake'
        const d = decideFor(rec); changed = true; return d
      }

      // ① fired/delivered：先查绑定本件的出站（有则 done）；fired 进了会话 → delivered；
      //    delivered 超 DONE_WINDOW_MS 没开口 → 带 silent 重投一次或 owed(silent)；fired 10 分钟没进会话 → 重投（P3/P7）
      for (const [id, rec] of Object.entries(store)) {
        if (!isUsableRecord(rec) || (rec.state !== 'fired' && rec.state !== 'delivered')) continue
        const firedAt = Number.isFinite(rec.fired_at) ? Number(rec.fired_at) : 0
        const bound = boundOutbound(id, rec, firedAt)
        if (bound) {
          rec.state = 'done'; rec.closed_at = t; counts.confirmed++; changed = true
          log(`promise_done id=${id} chat=${rec.chat_id} outbound_ms=${bound.ms}`)
          continue
        }
        const messageId = typeof rec.message_id === 'string' ? rec.message_id : ''
        if (rec.state === 'fired' && messageId && delivered.has(`${rec.chat_id}\u0000${messageId}`)) {
          rec.state = 'delivered'; rec.delivered_at = t; changed = true
          log(`promise_delivered id=${id} chat=${rec.chat_id}`)
          continue
        }
        const silent = rec.state === 'delivered'
        const sinceMs = silent && Number.isFinite(rec.delivered_at) ? Number(rec.delivered_at) : firedAt
        if (!sinceMs || t - sinceMs < (silent ? DONE_WINDOW_MS : RETRY_AFTER_MS)) continue
        const attempts = (Number.isFinite(rec.attempts) ? Number(rec.attempts) : 0) + 1
        const oldKey = typeof rec.inbox_key === 'string' ? rec.inbox_key : ''
        if (attempts >= MAX_ATTEMPTS) {
          rec.attempts = attempts; changed = true
          const reason: OwedReason = silent ? 'silent' : 'undeliverable'
          owedAdds.push({ chatId: rec.chat_id, entry: makeOwedEntry(id, rec, reason, t) })
          owedApply.push({ id, kind: silent ? 'silent' : 'failed', retractKey: oldKey })
          continue
        }
        // 重投前过同一套闸（R4）：暂停 / 判睡 / 探测失败一律不重投也不转 owed，保持原状、不加 attempts、撤旧件
        const gate = gateFor(rec)
        if (gate !== 'awake') {
          if (oldKey) { retracts.push({ chatId: rec.chat_id, key: oldKey }); rec.inbox_key = null; changed = true }
          if (gate === 'unknown') { counts.deferred++; log(`promise_deferred id=${id} chat=${rec.chat_id} reason=probe_unknown stage=retry`) }
          else defer(id, rec, gate, ' stage=retry')
          continue
        }
        const g = graceOrLate(rec, recentInbound(rec.chat_id), false)
        if (!g) continue
        rec.attempts = attempts; changed = true
        jobs.push(armFire(id, rec, t, silent ? 'silent' : g.late)); counts.retried++
        log(`promise_retry id=${id} chat=${rec.chat_id} attempts=${attempts}${silent ? ' kind=silent' : ''}`)
      }

      // ② 到期：顺延（暂停 / 判睡 / 探测失败）或发出。顺延超 DEFER_GIVEUP_MS 才转 owed；任何一支都不得改写 due_at（P2/P6）
      const due = Object.entries(store)
        .filter(([, r]) => isUsableRecord(r) && r.state === 'pending' && t >= r.due_at)
        .sort((a, b) => a[1].due_at - b[1].due_at)
      counts.due = due.length
      const firedChats = new Set<string>()
      for (const [id, rec] of due) {
        const gate = gateFor(rec)
        if (gate === 'unknown') {
          counts.deferred++
          log(`promise_deferred id=${id} chat=${rec.chat_id} reason=probe_unknown fails=${rec.probe_fails ?? 0}`)
          continue
        }
        if (gate !== 'awake') { defer(id, rec, gate, ''); continue }
        // 探测坏满 5 次那一刻即便窗口内也按"顺延结束"走起床缓冲（TESTFIX c），不立即发
        const g = graceOrLate(rec, recentInbound(rec.chat_id), probe === 'unknown' && Number(rec.probe_fails) >= PROBE_FAIL_MAX)
        if (!g) continue
        if (firedChats.has(rec.chat_id)) { counts.deferred++; rateLimited++; continue }   // 每 chat 每轮最多 1 条
        firedChats.add(rec.chat_id)
        jobs.push(armFire(id, rec, t, g.late)); counts.fired++; changed = true
        log(`promise_fire id=${id} chat=${rec.chat_id} late_ms=${Math.max(0, t - rec.due_at)}${g.late ? ` late_reason=${g.late}` : ''}`)
      }

      // ③ 欠账先落盘，成功了才改状态——写不进去就让它留在 pending 下轮重来，绝不静默消失
      const appendedOwed = owedAdds.length > 0 && appendOwed(owedAdds, t)   // 追加时可能把同聊天最旧的挤掉（§9-2）
      if (appendedOwed) {
        for (const a of owedApply) {
          const rec = store[a.id]
          if (!isUsableRecord(rec)) continue
          rec.state = 'owed'; changed = true
          if (a.retractKey) retracts.push({ chatId: rec.chat_id, key: a.retractKey })
          if (a.kind === 'failed' || a.kind === 'silent') counts.failed++; else counts.owed++
          log(`promise_owed id=${a.id} chat=${rec.chat_id} kind=${a.kind}`)
        }
      }

      // ⑥ 认账回执（worker 确认"带着欠账的那条"已进会话）：该聊天在回执之后有任何成功出站（ms ≥ at）→ settled 终态（R7）；
      //    不看 promise_ids（r2.2）：欠账挂在真人消息上，worker 拿不到条目 id，按绑定判永远了结不了。
      //    超 DONE_WINDOW_MS 仍无此类出站 → 按原 reason 重新挂账、丢弃回执；否则留到下轮（C-12）。只删已处理的回执
      const checkExpiry = expiryDue || appendedOwed
      const fl = receiptsDue || checkExpiry ? loadJsonForWrite(inflightPath, t, note => warn('STORE-CORRUPT', note)) : null
      const receipts: any[] = fl && Array.isArray(fl.settled) ? fl.settled : []
      const handled = new Set<any>()
      const rehang: { chatId: string; entry: OwedEntry }[] = []
      for (const r of receipts) {
        const pid = String(r?.promise_id)
        const rec = store[pid]
        if (!isUsableRecord(rec) || rec.state !== 'owed') { handled.add(r); continue }
        const at = Number.isFinite(r?.at) ? Number(r.at) : 0
        const o = outbound[rec.chat_id]
        if (o && o.ms >= at) {
          rec.state = 'settled'; rec.closed_at = t; changed = true; handled.add(r)
          log(`promise_settled id=${pid} chat=${rec.chat_id} outbound_ms=${o.ms}`)
        } else if (t - at > DONE_WINDOW_MS) {
          const reason: OwedReason = (OWED_REASONS as readonly string[]).includes(r?.reason) ? r.reason : 'undeliverable'
          rehang.push({ chatId: rec.chat_id, entry: makeOwedEntry(pid, rec, reason, t) })
          handled.add(r)
          log(`promise_owed id=${pid} chat=${rec.chat_id} kind=rehang reason=${reason}`)
        }
      }
      if (rehang.length && !appendOwed(rehang, t)) for (const x of rehang) handled.delete(receipts.find(r => String(r?.promise_id) === x.entry.promise_id))
      const receiptsChanged = handled.size > 0

      // ⑦ 欠账过期或被上限挤掉 → 终态 expired（§9-2），查询不再显示为欠着，参与 30 天清理。
      // 放在欠账追加（③，上限挤掉就发生在那里）与认账（⑥）之后；任一文件读不成（坏且备份不了）就整步跳过
      const owedNow = checkExpiry ? loadJsonForWrite(owedPath, t, note => warn('STORE-CORRUPT', note)) : null
      if (owedNow && fl) {
        for (const id of expiredOwedIds(store, owedNow, inflightMap(fl), settledIdsOf(fl), t)) {
          const rec = store[id]
          rec.state = 'expired'; rec.closed_at = t; changed = true
          log(`promise_expired id=${id} chat=${rec.chat_id}`)
        }
      }

      // ⑤ 终态满 30 天的记录删掉（R3）：bot 有记忆即可，gist 不无限期明文留盘
      let purged = 0
      for (const [id, rec] of Object.entries(store)) {
        if (!stopped && isUsableRecord(rec) && isPurgeable(rec, t)) { delete store[id]; purged++ }
      }
      if (purged) { changed = true; log(`promise_purge count=${purged}`) }

      // ④ 先落盘再注入：落不下去就一条都不发（重复发比漏发一次糟得多）
      if (changed && !persistStore(store)) { jobs = []; retracts.length = 0 }
      else if (fl && receiptsChanged) {
        // 账本落盘成功后才删已处理的回执：删失败只会下轮再过一遍（已是 settled 的跳过），不会漏
        fl.settled = receipts.filter(r => !handled.has(r))
        try { writeJsonAtomic(inflightPath, fl) } catch { /* 下轮重来 */ }
      }
      for (const r of retracts) {
        if (deps.retract) try { deps.retract(r.chatId, r.key) } catch { /* 撤不掉：worker 侧 30 分钟过期规则收尾 */ }
      }
    } finally { releaseLock(lockPath, tickToken) }

    if (rateLimited > 0) warnThrottled('RATE-LIMITED', `deferred=${rateLimited}`)
    for (const job of jobs) {
      if (job.retractKey && deps.retract) {
        try { deps.retract(job.chatId, job.retractKey) } catch { /* 撤不掉不影响重投 */ }
      }
      try { inject(job.chatId, job.key, job.meta) } catch (e) {
        log(`promise_inject_failed chat=${job.chatId} reason=${e instanceof Error ? e.name : 'unknown'}`)
      }
      throttledEnsureWorker(job.chatId, t)             // 异步拉活，不等待（worker 起来会 drain inbox）
    }
    if (hasAction(counts)) log(formatTickLine(counts))
  }

  /**
   * 一轮只扫一遍会话文件（S6），从所有 fired 记录里最早的发出偏移扫到文件末尾（R5），
   * 返回已在会话里出现的 `chat\0message_id`。判据 deliveryEvidence：user 行或中途交付行（absorbed_mid_turn /
   * queued_command）都算模型已见，与 worker 侧 settleSeen 口径一致；只有入队行（accepted）不算。
   */
  function findDelivered(store: PromiseStore): Set<string> {
    const want = new Map<string, string>()            // message_id → chat_id
    let from = Infinity
    for (const r of Object.values(store)) {
      if (!isUsableRecord(r) || r.state !== 'fired' || typeof r.message_id !== 'string' || !r.message_id) continue
      want.set(r.message_id, r.chat_id)
      from = Math.min(from, Number.isInteger(r.jsonl_offset) && Number(r.jsonl_offset) >= 0 ? Number(r.jsonl_offset) : 0)
    }
    const found = new Set<string>()
    if (!want.size) return found
    try {
      scanSession(from, line => {
        for (const [messageId, chatId] of want) {
          // 先按 id 字面粗筛（原始行里引号是转义的，不能带引号匹配），命中再交给契约判据精判
          if (!line.includes(messageId)) continue
          const ev = deliveryEvidence(line, { chatId, messageId })
          if (ev.user + ev.midTurn >= 1) {
            found.add(`${chatId}\u0000${messageId}`)
          }
        }
      })
    } catch { /* 读不到 → 判未送达 → 重投，绝不误判成已送达 */ }
    return found
  }

  return { handle, tick }
}
