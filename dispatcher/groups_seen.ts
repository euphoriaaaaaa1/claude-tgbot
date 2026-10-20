/**
 * 刚发现的群（bot-features 第三批 F3-1；INTERFACE-bot-features.md 3.4.5、3.10 第 5、7、8、12 条）。
 *
 * 记录存在 <频道目录>/groups-seen.json，调度器是唯一写者。这里只放能单测的部分：
 * 记录合并规则、50 条上限、60 秒限频、原子写。导入没有副作用：不读文件、不读环境变量。
 *
 * 文件里只有群号、群名、类型、状态、发现来源与时间；任何字段都不含消息正文、
 * 发送者用户名或编号。
 */
import { chmodSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs'
import { basename, dirname, join } from 'path'
import { randomBytes } from 'crypto'

export const GROUPS_SEEN_VERSION = 1
export const MAX_SEEN_GROUPS = 50
export const DEFAULT_MIN_INTERVAL_S = 60

export type SeenGroup = {
  title: string | null
  type: string | null
  status: string | null
  added_by_owner: boolean | null
  first_seen: string | null
  last_seen: string | null
  migrated_from: string | null
  left_in_console_at: string | null
}

export type GroupsSeenDoc = { version: 1; groups: Record<string, SeenGroup> }

export type GroupMessageInput = { chatId: string; title?: unknown; type?: unknown }

export type MyChatMemberInput = {
  chatId: string
  title?: unknown
  type?: unknown
  status?: unknown
  actorId?: unknown
  ownerIds: readonly string[]
  /** Telegram 的 chat.migrate_to_chat_id：有就是超级群迁移。 */
  migrateTo?: unknown
}

/** ISO UTC、秒精度：`2026-10-15T08:00:00Z`（不带毫秒/小数秒）。 */
export function isoSeconds(ms: number = Date.now()): string {
  return new Date(ms).toISOString().slice(0, 19) + 'Z'
}

/**
 * DISPATCHER_GROUPS_SEEN_MIN_INTERVAL_S（3.10 第 5 条）：整数字符串，缺省 60；
 * `0` 表示不限频；负数/非数字按 60。调用方每次写入前现读，不缓存。
 */
export function minIntervalSecondsFromEnv(raw: string | undefined): number {
  if (raw === undefined) return DEFAULT_MIN_INTERVAL_S
  const text = raw.trim()
  if (!/^-?\d+$/.test(text)) return DEFAULT_MIN_INTERVAL_S
  const n = Number(text)
  if (!Number.isSafeInteger(n) || n < 0) return DEFAULT_MIN_INTERVAL_S
  return n
}

const strOrNull = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)

function normalizeRecord(raw: Record<string, unknown>): SeenGroup {
  return {
    title: strOrNull(raw.title),
    type: strOrNull(raw.type),
    status: strOrNull(raw.status),
    added_by_owner: raw.added_by_owner === true ? true : raw.added_by_owner === false ? false : null,
    first_seen: strOrNull(raw.first_seen),
    last_seen: strOrNull(raw.last_seen),
    migrated_from: strOrNull(raw.migrated_from),
    left_in_console_at: strOrNull(raw.left_in_console_at),
  }
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v)

export const emptyDoc = (): GroupsSeenDoc => ({ version: GROUPS_SEEN_VERSION, groups: {} })

/** 读到的 JSON 不管是旧形状还是坏了，都收成当前形状；不认识的值按 null。 */
export function normalizeDoc(raw: unknown): GroupsSeenDoc {
  const doc = emptyDoc()
  if (!isObject(raw) || !isObject(raw.groups)) return doc
  for (const [chatId, rec] of Object.entries(raw.groups)) {
    if (!isObject(rec)) continue
    doc.groups[chatId] = normalizeRecord(rec)
  }
  return doc
}

/**
 * 读记录文件。文件不存在按空记录；读不出或不是合法 JSON 也按空记录，
 * 并把原因交给调用方记一行日志（不抛错，调度器不能因此崩）。
 */
export function readGroupsSeenFile(file: string): { doc: GroupsSeenDoc; error: string | null } {
  let text: string
  try {
    text = readFileSync(file, 'utf8')
  } catch (e) {
    if ((e as { code?: unknown } | null)?.code === 'ENOENT') return { doc: emptyDoc(), error: null }
    return { doc: emptyDoc(), error: e instanceof Error ? e.message : String(e) }
  }
  try {
    return { doc: normalizeDoc(JSON.parse(text)), error: null }
  } catch (e) {
    return { doc: emptyDoc(), error: e instanceof Error ? e.message : String(e) }
  }
}

/** 原子写 JSON：同目录临时文件 + rename；文件权限 0600。失败时清掉临时文件再抛出。 */
export function writeJsonAtomic(file: string, value: unknown): void {
  const tmp = join(dirname(file), `.${basename(file)}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`)
  try {
    writeFileSync(tmp, JSON.stringify(value) + '\n', { mode: 0o600 })
    chmodSync(tmp, 0o600)
    renameSync(tmp, file)
  } catch (e) {
    try { rmSync(tmp, { force: true }) } catch { /* 清不掉也不影响主流程 */ }
    throw e
  }
}

export function writeGroupsSeenFile(file: string, doc: GroupsSeenDoc): void {
  writeJsonAtomic(file, doc)
}

// ─── 写锁 ─────────────────────────────────────────────────────────────
// 公开版只有 dispatcher 一个写者（Python 侧不写本文件），进程内事件循环天然串行，
// 因此不再引入 macOS 专用的 flock。保留同名导出给调用方兜底，回调仍要求同步。
export function withGroupsSeenLock<T>(_file: string, fn: () => T): T {
  const value = fn()
  if (value && typeof (value as { then?: unknown }).then === 'function') {
    throw new Error('withGroupsSeenLock 的回调必须同步')
  }
  return value
}

/** 两条时间都取 last_seen，缺失或坏值按最旧。 */
function lastSeenMs(rec: SeenGroup): number {
  const t = rec.last_seen ? Date.parse(rec.last_seen) : NaN
  return Number.isFinite(t) ? t : 0
}

function firstSeenMs(rec: SeenGroup): number {
  const t = rec.first_seen ? Date.parse(rec.first_seen) : NaN
  return Number.isFinite(t) ? t : 0
}

/**
 * 上限 50：超出时挤掉 added_by_owner 不是 true 的记录里最旧的（last_seen，
 * 再按 first_seen、群号）。主人拉的群优先保留；全都标记为 true 时只能挤最旧的。
 */
export function capGroups(doc: GroupsSeenDoc): void {
  let keys = Object.keys(doc.groups)
  while (keys.length > MAX_SEEN_GROUPS) {
    const entries = keys.map(k => [k, doc.groups[k]] as const)
    const candidates = entries.filter(([, rec]) => rec.added_by_owner !== true)
    const pool = candidates.length > 0 ? candidates : entries
    pool.sort((a, b) => {
      const dl = lastSeenMs(a[1]) - lastSeenMs(b[1])
      if (dl !== 0) return dl
      const df = firstSeenMs(a[1]) - firstSeenMs(b[1])
      if (df !== 0) return df
      return a[0].localeCompare(b[0])
    })
    delete doc.groups[pool[0][0]]
    keys = Object.keys(doc.groups)
  }
}

/**
 * 群消息带来的发现/更新（3.4.5）：
 * - 陌生群（createIfMissing=true）建一条，added_by_owner=null、status=null；
 * - 已有记录只更新 title 与 last_seen，不改 added_by_owner（含 null）、status、
 *   migrated_from、left_in_console_at、first_seen；
 * - 同一群两次写入间隔小于 minIntervalS 秒时整次跳过（不重写文件）；
 *   minIntervalS=0 表示不限频。返回是否要写文件。
 */
export function applyGroupMessage(
  doc: GroupsSeenDoc,
  msg: GroupMessageInput,
  nowMs: number,
  minIntervalS: number,
  createIfMissing: boolean,
): boolean {
  const chatId = String(msg.chatId)
  const rec = doc.groups[chatId]
  if (!rec && !createIfMissing) return false
  if (rec && minIntervalS > 0) {
    const last = rec.last_seen ? Date.parse(rec.last_seen) : NaN
    if (Number.isFinite(last) && nowMs - last < minIntervalS * 1000) return false
  }
  const now = isoSeconds(nowMs)
  if (!rec) {
    doc.groups[chatId] = {
      title: strOrNull(msg.title),
      type: strOrNull(msg.type),
      status: null,
      added_by_owner: null,
      first_seen: now,
      last_seen: now,
      migrated_from: null,
      left_in_console_at: null,
    }
  } else {
    if (typeof msg.title === 'string' && msg.title !== '') rec.title = msg.title
    rec.last_seen = now
  }
  capGroups(doc)
  return true
}

/**
 * my_chat_member（3.4.5、3.10 第 7 条）：
 * - 身份变化不受限频，每次都写；
 * - added_by_owner：记录里已是 true/false 就保持（先建的真/假都算定案）；
 *   原是 null 才按执行者是否在私聊白名单改成 true/false；
 * - status 随 new_chat_member.status 更新，title/type 随 chat 更新；
 *   不写、不清 left_in_console_at；
 * - 超级群迁移只写新群号一条：有旧记录时 chat.id 是旧群号、migrate_to 是新群号并
 *   复制旧记录；没有旧记录时 chat.id 已是新群号、migrate_to 是旧群号，用消息里的
 *   title/type（type 缺省 supergroup），status="member"、added_by_owner=null。
 *   旧记录原样保留。
 */
export function applyMyChatMember(doc: GroupsSeenDoc, ev: MyChatMemberInput, nowMs: number): boolean {
  const now = isoSeconds(nowMs)
  const chatId = String(ev.chatId)

  if (ev.migrateTo != null && String(ev.migrateTo) !== '') {
    const otherId = String(ev.migrateTo)
    const oldRec = doc.groups[chatId] ?? null
    const newId = oldRec ? otherId : chatId
    const fromId = oldRec ? chatId : otherId
    doc.groups[newId] = {
      title: oldRec ? oldRec.title : strOrNull(ev.title),
      type: oldRec ? oldRec.type : (strOrNull(ev.type) ?? 'supergroup'),
      status: oldRec ? oldRec.status : 'member',
      added_by_owner: oldRec ? oldRec.added_by_owner : null,
      first_seen: now,
      last_seen: now,
      migrated_from: fromId,
      left_in_console_at: null,
    }
    capGroups(doc)
    return true
  }

  const status = strOrNull(ev.status) ?? 'member'
  const actor = ev.actorId == null ? '' : String(ev.actorId)
  const byOwner = ev.ownerIds.some(id => String(id) === actor)
  const rec = doc.groups[chatId]
  if (!rec) {
    doc.groups[chatId] = {
      title: strOrNull(ev.title),
      type: strOrNull(ev.type),
      status,
      added_by_owner: byOwner,
      first_seen: now,
      last_seen: now,
      migrated_from: null,
      left_in_console_at: null,
    }
  } else {
    if (typeof ev.title === 'string' && ev.title !== '') rec.title = ev.title
    if (typeof ev.type === 'string' && ev.type !== '') rec.type = ev.type
    rec.status = status
    if (rec.added_by_owner === null) rec.added_by_owner = byOwner
    rec.last_seen = now
  }
  capGroups(doc)
  return true
}
