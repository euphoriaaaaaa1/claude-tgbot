/**
 * 承诺兑现 · 接线件：把契约 §6.3 / §6.4 规定的三个注入点做成单一事实源，
 * dispatcher 与独立服务共用同一份，免得两边行为漂移。
 *
 * - `PROMISE_FORCE_ASLEEP`：只顶替"作息判睡"这一路，不碰 `.last-inbound-ts.json` 那一路
 * - `PROMISE_JSONL_PATH`：把送达判据读的会话文件换成指定路径（判据函数不变，语义等价）
 * 以上两个只在 `PROMISE_TEST_MODE=1` 时生效（见 testHook）
 * - `DISABLED_DIR`：整体关停开关 `<DISABLED_DIR>/promise-<bot>` 的所在目录
 *
 * 铁律：这里给出的 probe 是**异步**的。tick 里绝不允许 spawnSync——dispatcher 是 4 个 bot
 * 的命脉，同步等子进程就是全线失联。
 */
import { closeSync, existsSync, fsyncSync, openSync, readFileSync, readSync, readdirSync, renameSync, rmSync, statSync, writeSync } from 'fs'
import { randomBytes } from 'crypto'
import { homedir } from 'os'
import { join, basename } from 'path'
import { spawn } from 'child_process'
import { isAsleep } from './hang_runtime'
import { sessionJsonlPath, unifiedSessionUuid } from './worker-manager'
import { DONE_WINDOW_MS, sanitizePromiseIds, type ProbeOutcome } from './promise_plan'

/** 作息探测脚本取本检出内的副本（§12-2）：开发与测试时绝不调生产目录那份；部署后二者是同一个目录 */
export const HANG_SITUATION_PY = join(import.meta.dir, '..', 'hang_situation.py')
const BOTLIFE_DIR = join(import.meta.dir, '..')
export const PROBE_TIMEOUT_MS = 3000
/** 探测解释器：显式 PYTHON_BIN 优先，否则 PATH 里的 python3（跨平台约定）。 */
export const PYTHON_BIN = process.env.PYTHON_BIN || 'python3'

/** 作息配置目录：与 config_loader.config_dir() 同一优先级（环境变量非空串优先），每次现读 */
export function botConfigDir(): string {
  const env = process.env.CLAUDEBOT_CONFIG_DIR
  return env ? env : join(BOTLIFE_DIR, 'configs')
}

/** 该 bot 有没有作息配置 `<configs>/<bot>.yml`（普通文件）。坏 bot 名 / 任何异常 → false；只读不建目录 */
export function hasBotSchedule(botName: unknown): boolean {
  if (typeof botName !== 'string' || !botName || /[/\\]/.test(botName) || botName.includes('..')) return false
  try { return statSync(join(botConfigDir(), `${botName}.yml`)).isFile() } catch { return false }
}

/**
 * 原子写 JSON（P8）：open→write→fsync→close→rename，落盘即 0600；失败清 tmp 并抛（调用方照旧 catch）。
 * tmp 名必须唯一：两个进程共用同一个 .tmp 时，一方 rename 走的是另一方的内容（R1）。
 */
export function writeJsonAtomic(path: string, data: unknown): void {
  const tmp = `${path}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  let fd = -1
  try {
    fd = openSync(tmp, 'w', 0o600)
    writeSync(fd, JSON.stringify(data))
    fsyncSync(fd)
    closeSync(fd); fd = -1
    renameSync(tmp, path)
  } catch (e) {
    if (fd >= 0) try { closeSync(fd) } catch {}
    try { rmSync(tmp, { force: true }) } catch {}
    throw e
  }
}

/** 出站事实源（P3）：`{ "<chat_id>": { ms, promise_ids } }`，dispatcher /send 成功出口写，runtime 只读 */
export const LAST_OUTBOUND_FILE = '.last-outbound-ts.json'
export type OutboundEntry = { ms: number; promise_ids: string[] }

/** 读出站表：旧形态（值为数字）按 `{ms, promise_ids: []}`；文件缺失 / 坏 JSON / 坏条目按空表 / 丢弃 */
export function readLastOutbound(channelDir: string): Record<string, OutboundEntry> {
  const out: Record<string, OutboundEntry> = {}
  let doc: unknown
  try { doc = JSON.parse(readFileSync(join(channelDir, LAST_OUTBOUND_FILE), 'utf8')) } catch { return out }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return out
  for (const [chat, v] of Object.entries(doc as Record<string, unknown>)) {
    if (typeof v === 'number' && Number.isFinite(v)) { out[chat] = { ms: v, promise_ids: [] }; continue }
    const o = v as { ms?: unknown; promise_ids?: unknown }
    if (o && typeof o === 'object' && typeof o.ms === 'number' && Number.isFinite(o.ms)) {
      out[chat] = { ms: o.ms, promise_ids: sanitizePromiseIds(o.promise_ids) }
    }
  }
  return out
}

/**
 * 记一次成功出站。同步函数（读→改→writeJsonAtomic，无 await）：dispatcher 单进程事件循环里并发 /send
 * 天然串行，runtime 只读不写，故无第二写者。任何失败 false 不抛。
 * 合并规则（r2.2）：该 chat 旧记录距今 ≤ DONE_WINDOW_MS → promise_ids = 旧 ∪ 新（新值先清洗，去重保序）；
 * 否则 = 新。同一轮连发两条 reply 时第二条带 [] 不得冲掉第一条的绑定。ms 一律取 nowMs。
 */
export function markOutbound(channelDir: string, chatId: string, promiseIds: unknown, nowMs = Date.now()): boolean {
  try {
    if (!channelDir || !chatId) return false
    const doc = readLastOutbound(channelDir)
    const key = String(chatId)
    const fresh = sanitizePromiseIds(promiseIds)
    const prev = doc[key]
    const ids = prev && nowMs - prev.ms <= DONE_WINDOW_MS ? sanitizePromiseIds([...prev.promise_ids, ...fresh]) : fresh
    doc[key] = { ms: nowMs, promise_ids: ids }
    writeJsonAtomic(join(channelDir, LAST_OUTBOUND_FILE), doc)
    return true
  } catch { return false }
}

/**
 * bot 的通道目录。给的可能已经是 bot 目录本身（生产：CHANNEL_DIR=~/.claude/channels/<bot>），
 * 也可能是放着多个 bot 的通道根目录。按"目录名是否就是 bot 名 / 有没有同名子目录"判定。
 */
export function resolvePromiseChannelDir(channelDir: string, botName: string): string {
  if (!channelDir) return ''
  if (!botName || basename(channelDir) === botName) return channelDir
  const nested = join(channelDir, botName)
  return existsSync(nested) ? nested : channelDir
}

/**
 * 撤掉一件还没被 worker 消费的 inbox 文件（重投前用）。第二参是写件时的 inbox key（promise<ms>），
 * 文件名主干由这里拼，规则必须与 dispatcher.ts:writeInbox 逐字一致。
 * 连 `.pending/<name>.json` 与 collision 改名形态 `.pending/<ts>-<name>.json`（精确正则，不用通配）一并撤（P5）；
 * 已被 worker 读进内存的件撤不到，由 worker 侧按 promise_id 去重兜住。
 */
export function makeInboxRetract(channelDir: string): (chatId: string, key: string) => void {
  return (chatId, key) => {
    const name = `${chatId}_${key}`.replace(/[^a-zA-Z0-9_-]/g, '_')
    const pending = join(channelDir, 'inbox', '.pending')
    try { rmSync(join(channelDir, 'inbox', `${name}.json`), { force: true }) } catch {}
    try { rmSync(join(pending, `${name}.json`), { force: true }) } catch {}
    try {
      const re = new RegExp(`^\\d+-${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.json$`)
      for (const f of readdirSync(pending)) if (re.test(f)) try { rmSync(join(pending, f), { force: true }) } catch {}
    } catch { /* .pending 不存在按无项 */ }
  }
}

/**
 * bot 停用开关（E9）：`.disabled/<bot>` 存在 → 与暂停同一道闸：到期顺延（不改 due_at），
 * 重投不注入、转欠账 paused（契约 §8 第 2 条）。返回非 null 的原因码，与 isPaused 同形。
 */
export function makeBotDisabledCheck(botName: string): () => string | null {
  const dir = process.env.DISABLED_DIR || join(homedir(), '.claude', 'dispatcher', '.disabled')
  const path = join(dir, botName)
  return () => { try { return botName && existsSync(path) ? 'bot_disabled' : null } catch { return null } }
}

/**
 * 测试模式（promise_service，PROMISE_TEST_MODE=1）的暂停闸：只认 PROMISE_PAUSE_FILE 显式指向的测试文件，
 * 没设或文件不存在 = 未暂停。绝不读真实家目录下的 ~/.claudebotlife.pause 与 ~/.claudebotlife.pause-<bot>——
 * 那是真实 bot 的运行状态，用户暂停真 bot 期间跑验收，所有送达用例都会被顺延而失败。生产 dispatcher 不走这里。
 */
export function makeTestPauseCheck(env: Record<string, string | undefined> = process.env): () => string | null {
  const file = env.PROMISE_PAUSE_FILE || ''
  return () => { try { return file && existsSync(file) ? 'test_pause_file' : null } catch { return null } }
}

/** 关停开关（I6）：存在即整体停止，已登记数据不动。 */
export function makeStopCheck(botName: string): () => boolean {
  const dir = process.env.DISABLED_DIR || join(homedir(), '.claude', 'dispatcher', '.disabled')
  const path = join(dir, `promise-${botName}`)
  return () => { try { return existsSync(path) } catch { return false } }
}

/**
 * 测试注入口只在 PROMISE_TEST_MODE=1 时生效（安审 B5）：生产 dispatcher 若被带着测试 shell 的 env
 * 重启，FORCE_ASLEEP=0 会让半夜也发、JSONL_PATH 指错会让每条承诺重发三次。被忽略时打一行（每个变量一次）。
 */
const ignoredHooks = new Set<string>()
export function testHook(name: 'PROMISE_FORCE_ASLEEP' | 'PROMISE_JSONL_PATH'): string {
  const v = process.env[name] || ''
  if (!v || process.env.PROMISE_TEST_MODE === '1') return v
  if (!ignoredHooks.has(name)) {
    ignoredHooks.add(name)
    process.stderr.write(`promise_wire: test_hook_ignored name=${name} reason=not_test_mode\n`)
  }
  return ''
}

const SCAN_CHUNK = 1024 * 1024
const NEEDLE = Buffer.from('message_id=')   // 原始 jsonl 里引号是转义的（message_id=\"…\"），只匹配到等号为止

/**
 * 从字节偏移 fromOffset 起按行扫描 jsonl 到 EOF（R5：确认不能只看末尾一段——兑现时的图片/
 * 工具结果单行就能有 MB 级，会把那条 user 行挤出任何固定大小的尾窗）。
 * fromOffset 非法或大于文件当前大小（文件被换/截断）→ 从头扫。按字节切行，多字节字符跨块不会被劈开；
 * 不含 `message_id=` 的行（绝大多数，含 base64 大行）不转字符串，直接跳过。读不到就一行都不回调。
 */
export function scanJsonlFrom(path: string, fromOffset: number, onLine: (line: string) => void): void {
  if (!path) return
  let fd = -1
  try {
    const size = statSync(path).size
    let pos = Number.isInteger(fromOffset) && fromOffset >= 0 && fromOffset <= size ? fromOffset : 0
    fd = openSync(path, 'r')
    const buf = Buffer.allocUnsafe(SCAN_CHUNK)
    let carry: Buffer[] = []
    const emit = (line: Buffer) => { if (line.includes(NEEDLE)) onLine(line.toString('utf8')) }
    while (pos < size) {
      const n = readSync(fd, buf, 0, Math.min(SCAN_CHUNK, size - pos), pos)
      if (n <= 0) break
      pos += n
      let start = 0
      for (let nl = buf.indexOf(10, start); nl !== -1 && nl < n; nl = buf.indexOf(10, start)) {
        const piece = buf.subarray(start, nl)
        emit(carry.length ? Buffer.concat([...carry, piece]) : piece)
        carry = []
        start = nl + 1
      }
      if (start < n) carry.push(Buffer.from(buf.subarray(start, n)))   // 拷出来：buf 下一轮会被覆盖
    }
    if (carry.length) emit(Buffer.concat(carry))
  } catch { /* 读不到 → 不回调 → 判未送达，绝不误判成已送达 */ }
  finally { if (fd >= 0) try { closeSync(fd) } catch {} }
}

/** 会话 jsonl 的大小与扫描器。取不到路径/文件时 size=-1、扫描不回调（→ 判未送达 → 重投）。 */
export function makeSessionScanner(channelDir: string, botName: string, pathOverride = ''): {
  sessionSize: () => number
  scanSession: (fromOffset: number, onLine: (line: string) => void) => void
} {
  let path = pathOverride || testHook('PROMISE_JSONL_PATH')
  if (!path) {
    try { path = sessionJsonlPath(channelDir, unifiedSessionUuid(botName)) } catch { path = '' }
  }
  return {
    sessionSize: () => { if (!path) return -1; try { return statSync(path).size } catch { return -1 } },
    scanSession: (fromOffset, onLine) => scanJsonlFrom(path, fromOffset, onLine),
  }
}

/**
 * 作息判睡探测（P2/P10）：四值 ProbeOutcome。无作息配置 → unsupported（不起子进程）；退出 0 且 JSON 含
 * state 且 schedule_ok!==false → asleep/awake；超时 3s / 退出非 0 / JSON 坏 / schedule_ok:false → unknown，
 * 并经 log 写一行 `promise_probe_stderr bot= code= text=`（stderr 截 200 字、换行换空格；脚本只打类名无正文）。
 * atMs 有值则第 2 参传 Math.floor(atMs/1000)（登记告知用）。全异步：spawn + 超时 kill，事件循环一刻都不阻塞。
 */
export function makeAsleepProbe(
  botName: string, scriptPath = HANG_SITUATION_PY, cwd = BOTLIFE_DIR,   // 后三个参数只供单测注入，生产用默认值
  log: (line: string) => void = () => {},
): (atMs?: number) => Promise<ProbeOutcome> {
  return (atMs?: number) => new Promise<ProbeOutcome>(resolve => {
    // 测试注入口先于"有无作息配置"：测试里的假 bot 没有 yml，排在后面就永远轮不到，结论跟着运行时刻的兜底窗口走
    //（夜里跑必挂）。testHook 只在 PROMISE_TEST_MODE=1 时生效，生产不受影响。
    const force = testHook('PROMISE_FORCE_ASLEEP')
    if (force === '1') return resolve('asleep')
    if (force === '0') return resolve('awake')
    if (!hasBotSchedule(botName)) return resolve('unsupported')
    let settled = false
    const finish = (v: ProbeOutcome) => { if (!settled) { settled = true; resolve(v) } }
    let out = '', err = ''
    const fail = (code: number | string) => {
      const text = err.replace(/\s+/g, ' ').trim().slice(0, 200)
      try { log(`promise_probe_stderr bot=${botName} code=${code} text=${text}`) } catch {}
      finish('unknown')
    }
    try {
      const args = [scriptPath, botName]
      if (typeof atMs === 'number' && Number.isFinite(atMs)) args.push(String(Math.floor(atMs / 1000)))
      const p = spawn(PYTHON_BIN, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] })
      p.stdout?.on('data', (d: Buffer) => { if (out.length < 64 * 1024) out += d.toString('utf8') })
      p.stderr?.on('data', (d: Buffer) => { if (err.length < 4096) err += d.toString('utf8') })
      const timer = setTimeout(() => { try { p.kill('SIGKILL') } catch {} ; fail('timeout') }, PROBE_TIMEOUT_MS)
      p.on('error', () => { clearTimeout(timer); fail('spawn_error') })
      p.on('close', (code: number | null) => {
        clearTimeout(timer)
        if (code !== 0 || !out.trim()) return fail(code ?? 'null')
        try {
          const o = JSON.parse(out)
          // 作息表缺失/不是字典/空文件时脚本报 schedule_ok:false：当探测失败 → unknown（§12-2）
          if (o && o.schedule_ok !== false && typeof o.state === 'string') return finish(isAsleep(o.state) ? 'asleep' : 'awake')
          fail(code)
        } catch { fail(code) }
      })
    } catch { fail('spawn_error') }
  })
}

// ─── 送达判据（与 worker-plugin 的投递确认同源，纯函数）────────────────────
export type DeliveryKey = { chatId: string; messageId: string }
export type DeliveryEvidence = { user: number; midTurn: number; accepted: number }

const CHANNEL_OPEN_RE = /^\s*<channel\b[^>]*>/

/** 开标签属性匹配：只比对 <channel …> 开标签里的 chat_id/message_id，不读正文。 */
function openTagHasKey(s: unknown, needChat: string, needMsg: string): boolean {
  if (typeof s !== 'string') return false
  const m = CHANNEL_OPEN_RE.exec(s)
  if (!m) return false
  return m[0].includes(needChat) && m[0].includes(needMsg)
}

/**
 * 一次扫描给出三种证据：user 行数、中途交付数、入队行数。
 * user + midTurn >= 1 即"模型已见"。只比对开标签属性，不读正文。
 */
export function deliveryEvidence(jsonlText: string, key: DeliveryKey): DeliveryEvidence {
  const zero: DeliveryEvidence = { user: 0, midTurn: 0, accepted: 0 }
  if (typeof jsonlText !== 'string' || !jsonlText) return zero
  const chatId = key?.chatId
  const messageId = key?.messageId
  if (typeof chatId !== 'string' || !chatId) return zero
  if (typeof messageId !== 'string' || !messageId) return zero
  const needChat = ` chat_id="${chatId}"`
  const needMsg = ` message_id="${messageId}"`

  let user = 0, absorbed = 0, queuedCmd = 0, accepted = 0
  for (const line of jsonlText.split('\n')) {
    if (!line.trim()) continue
    let row: any
    try { row = JSON.parse(line) } catch { continue }
    if (!row || typeof row !== 'object') continue
    if (row.origin?.kind === 'channel') {
      if (openTagHasKey(row.message?.content, needChat, needMsg)) user++
    } else if (row.type === 'queue-operation') {
      if (row.operation === 'enqueue') {
        if (openTagHasKey(row.content, needChat, needMsg)) accepted++
      } else if (row.operation === 'remove' && row.reason === 'absorbed_mid_turn') {
        if (openTagHasKey(row.content, needChat, needMsg)) absorbed++
      }
    } else if (row.type === 'attachment' && row.attachment?.type === 'queued_command') {
      if (openTagHasKey(row.attachment.prompt, needChat, needMsg)) queuedCmd++
    }
  }
  return { user, midTurn: Math.max(absorbed, queuedCmd), accepted }
}
