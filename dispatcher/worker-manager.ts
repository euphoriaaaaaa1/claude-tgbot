/**
 * worker-manager.ts — 跨平台 worker 生命周期管理。
 *
 * worker = 常驻 headless claude 子进程：
 *   claude -p --input-format stream-json --output-format stream-json
 * 入站：inbox 文件（dispatcher/director/moments/self-initiate 照旧写）→ 本模块
 * fs.watch → 组装（关系提示+场景标签+meta）→ 写 claude stdin。
 * 出站：worker 里的 MCP reply 工具 → dispatcher /send（不变）。
 * slash：/compact 等当 user 消息写 stdin（无需终端注入）。
 * 就绪：stdout 的 system/init 事件（无需 capture-pane 抓屏）。
 * 观测：logs/chat.log（人话对话流，tail -f 观察）+ logs/stream.jsonl（原始事件）。
 *
 * 与 dispatcher.ts 同进程（1 dispatcher : 1 worker）。
 */

import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from 'child_process'
import { createHash } from 'crypto'
import {
  readFileSync, writeFileSync, appendFileSync, readdirSync, rmSync, renameSync,
  mkdirSync, existsSync, statSync, watch, chmodSync, unlinkSync, realpathSync, type FSWatcher,
} from 'fs'
import { backupSessionJsonl } from './backup_retention'
import { join, delimiter, dirname, basename } from 'path'
import { homedir, tmpdir, platform } from 'os'
import { buildTimePrefix } from './time_annotate'
import { settingsPath } from './provider_watch'
import { createBurstCollector, readBurstCfg } from './burst_inbox'
import { takeHangArchive } from './hang_runtime'
import { situAnchorLine } from './situation_bridge'
import { applyInboundMarks, loadInboundState, testModeCheck, escapeUserMarkers, isPromiseDueInbox } from './chat_guard'
import { nextLastHuman, type LastHumanMessage } from './promise_plan'
import { photoDeliveryKeyFromMeta } from './photo_mode'
import { planInboxBatch, readInboxMeta } from './inbox_names'
import { escapeChannelWrapper } from './channel_wrap'
import { winCmdSpawnSpec, probeClaudeWin } from './win_cmd'

// ─── 配置（与 dispatcher.ts 同源的 env）────────────────────────────────
const BOT = process.env.BOT_NAME || ''
const CHANNEL_DIR = process.env.CHANNEL_DIR || join(homedir(), '.claude', 'channels', BOT)
const DISPATCHER_URL = `http://127.0.0.1:${process.env.DISPATCHER_PORT || '17801'}`

// ─── UUIDv5（纯 TS，替代两处 python3 shellout）─────────────────────────
// **必须与 chat_history.py 的 _BOT_NAMESPACES 完全一致**，否则丢记忆。
export const BOT_NAMESPACES: Record<string, string> = {
  chenlulu: '550e8400-e29b-41d4-a716-446655440001', // scan-release: allow legacy-compat
}

export function uuidv5(namespace: string, name: string): string {
  const ns = Buffer.from(namespace.replace(/-/g, ''), 'hex')
  const h = createHash('sha1').update(ns).update(name).digest()
  const b = Buffer.from(h.subarray(0, 16))
  b[6] = (b[6] & 0x0f) | 0x50 // version 5
  b[8] = (b[8] & 0x3f) | 0x80 // RFC4122 variant
  const s = b.toString('hex')
  return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`
}

export function unifiedSessionUuid(bot: string): string {
  // env BOT_NAMESPACE 优先（部署时免改源码；与 chat_history.py 的表保持一致仍是你的责任）
  const ns = process.env.BOT_NAMESPACE || BOT_NAMESPACES[bot]
  if (!ns) throw new Error(`no UUIDv5 namespace for bot '${bot}' — set env BOT_NAMESPACE or add to BOT_NAMESPACES`)
  return uuidv5(ns, 'unified')
}

// ─── project slug（官方规则：非字母数字全部替换为 '-'）──────────────────
// mac:  /Users/you/.claude/channels/bot → -Users-you--claude-channels-bot
// win:  C:\Users\user\.claude\channels\bot → C--Users-user--claude-channels-bot
// ⚠️ Windows 真实规则以 Phase 0 实测为准；spawn 后有断言兜底（不匹配 fail-loud）。
export function projectSlug(absDir: string): string {
  return absDir.replace(/[^a-zA-Z0-9]/g, '-')
}

// ─── H-25：会话文件路径与会话身份核对 ──────────────────────────────────
/** H-25：会话 jsonl 路径。channelDir 先做 realpath（符号链接用真实路径编码）；CLAUDE_CONFIG_DIR
 *  存在 → `<configDir>/projects/<slug>/<id>.jsonl`，否则 `<homedir>/.claude/projects/...`。 */
export function sessionJsonlPath(channelDir: string, sessionId: string, _opts?: Record<string, unknown>): string {
  let real = channelDir
  try { real = realpathSync(channelDir) } catch { /* 目录不存在等 → 用原样 */ }
  const slug = projectSlug(real)
  const cfg = process.env.CLAUDE_CONFIG_DIR
  // 用 process.env.HOME 优先于 os.homedir()：bun 下 homedir() 可能取进程启动时快照，测试注入 HOME 后取不到。
  const home = process.env.HOME || homedir()
  const root = (typeof cfg === 'string' && cfg.trim()) ? cfg : join(home, '.claude')
  return join(root, 'projects', slug, `${sessionId}.jsonl`)
}

export type IdentityStatus = 'pending' | 'match' | 'mismatch'
export type IdentityDecision = { status: IdentityStatus; deliver: boolean; adoptedId?: string }

/**
 * H-25：会话身份核对（纯函数，默认不阻断）。**同类相比**（id 对 id），三态：
 * - `pending`：CLI 尚未回报任何会话信息 → 不阻断，照常投递。
 * - `match`：id 相同 → 照常。
 * - `mismatch`：id 不同 → **只跳过本次投递**（消息留在队列，下一轮再核对，非永久停）。
 *   自愈：CLI 回报的 id 能在 `projectsDir` 下找到 `<id>.jsonl` → 直接采用该 id（不报 mismatch）。
 * `SESSION_IDENTITY_CHECK=off` → 完全跳过核对，只记一行 `wm_session_identity_check_off`。
 */
export function checkSessionIdentity(
  expectedId: string,
  cliId: string | null | undefined,
  opts: { projectsDir?: string; log?: (l: string) => void } = {},
): IdentityDecision {
  const log = typeof opts.log === 'function' ? opts.log : (l: string) => process.stderr.write(`${l}\n`)
  if (process.env.SESSION_IDENTITY_CHECK === 'off') {
    log('wm_session_identity_check_off')
    return { status: 'pending', deliver: true }
  }
  if (cliId == null || String(cliId) === '') return { status: 'pending', deliver: true }
  const got = String(cliId)
  if (got === String(expectedId)) return { status: 'match', deliver: true }
  if (opts.projectsDir) {
    try {
      if (existsSync(join(opts.projectsDir, `${got}.jsonl`))) {
        return { status: 'match', deliver: true, adoptedId: got }   // 自愈：采用 CLI 回报的 id
      }
    } catch { /* ignore */ }
  }
  log(`wm_session_mismatch expected_id=${expectedId} got_id=${got}`)
  return { status: 'mismatch', deliver: false }
}

// ─── 跨平台按进程树杀 ──────────────────────────────────────────────────
// Windows 上 worker 经 cmd /c 包装 → proc.pid 是 cmd 的，claude 是它的子进程。
// process.kill(cmd_pid) 只杀 cmd、留孤儿 claude(继续写同一 jsonl=记忆损坏)。必须按树杀。
export function killTree(pid: number, force = true): void {
  if (!pid || pid <= 0) return
  if (platform() === 'win32') {
    // Windows 无 /F 对控制台进程基本无效 → 恒带 /F；身份校验交给调用方（孤儿路径）
    try { spawnSync('taskkill', ['/PID', String(pid), '/T', '/F']) } catch {}
  } else {
    try { process.kill(pid, force ? 'SIGKILL' : 'SIGTERM') } catch {}
  }
}

// ─── 进程身份信息（孤儿校验用；注入点，测试给假件）───────────────────────
export interface ProcInfo { args: string[]; startedAtMs: number }

// 真实现：拿进程命令行 + 启动时间。任何一步失败/拿不到 → null（调用方一律"不杀"）。
// Windows tasklist 无启动时间字段 → 恒 null（退化为"拿不到就不杀"，契约 §三）。
export function realProcInfo(pid: number): ProcInfo | null {
  if (!pid || pid <= 0) return null
  if (platform() === 'win32') return null
  try {
    const a = spawnSync('ps', ['-o', 'args=', '-p', String(pid)], { encoding: 'utf8' })
    if (a.status !== 0 || !a.stdout) return null
    const t = spawnSync('ps', ['-o', 'lstart=', '-p', String(pid)], { encoding: 'utf8' })
    const startedAtMs = Date.parse((t.stdout || '').trim())
    if (!Number.isFinite(startedAtMs)) return null
    const args = a.stdout.trim().split(/\s+/).filter(Boolean)
    if (args.length === 0) return null
    return { args, startedAtMs }
  } catch { return null }
}

// H-03：孤儿清理前的身份证明。四条件全真才允许杀；任一不满足或取不到进程信息 → 不杀（保守）。
export function shouldKillOrphan(
  pid: number,
  recorded: any,
  currentUuid: string,
  procInfo: ProcInfo | null,
): { kill: boolean; reason: string } {
  let verdict: { kill: boolean; reason: string }
  if (!pid || pid <= 0) verdict = { kill: false, reason: 'bad_pid' }
  else if (!recorded || typeof recorded !== 'object' || Array.isArray(recorded)
    || typeof recorded.sessionUuid !== 'string' || !recorded.sessionUuid) {
    verdict = { kill: false, reason: 'legacy' }   // 旧格式（纯数字/无 sessionUuid）
  } else if (recorded.sessionUuid !== currentUuid) verdict = { kill: false, reason: 'uuid_mismatch' }
  else if (!procInfo) verdict = { kill: false, reason: 'no_proc_info' }
  else if (!Array.isArray(procInfo.args)
    || !procInfo.args.some(a => typeof a === 'string' && a.includes(currentUuid))) {
    verdict = { kill: false, reason: 'args_mismatch' }
  } else if (typeof procInfo.startedAtMs !== 'number'
    || procInfo.startedAtMs > Number(recorded.startedAtMs) + 5000) {
    verdict = { kill: false, reason: 'too_new' }
  } else verdict = { kill: true, reason: 'ok' }
  if (verdict.kill) logSpawn(`wm_orphan_kill pid=${pid}`)
  else logSpawn(`wm_orphan_skip reason=${verdict.reason}`)
  return verdict
}

// ─── claude 可执行解析 ─────────────────────────────────────────────────
export function resolveClaude(): { bin: string; viaCmd: boolean } {
  const override = process.env.CLAUDE_BIN
  if (override) return { bin: override, viaCmd: /\.(cmd|bat)$/i.test(override) }
  if (platform() === 'win32') {
    // 不用 `where claude`：它的 stdout 是 OEM 码页字节，中文用户名按 utf8 解成 U+FFFD → 路径不存在（见 win_cmd.probeClaudeWin）
    const found = probeClaudeWin(process.env)
    if (found) return { bin: found, viaCmd: /\.(cmd|bat)$/i.test(found) }
    return { bin: 'claude', viaCmd: false } // 交给 PATH，起不来会在 spawn error 里报清楚
  }
  const probe = spawnSync('which', ['claude'], { encoding: 'utf8' })
  const found = (probe.stdout || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean)[0]
  if (found) return { bin: found, viaCmd: false }
  return { bin: 'claude', viaCmd: false }
}

// ─── 日志 ──────────────────────────────────────────────────────────────
const LOG_DIR = join(CHANNEL_DIR, 'logs')
const CHAT_LOG = join(LOG_DIR, 'chat.log')
const STREAM_LOG = join(LOG_DIR, 'stream.jsonl')
const SPAWN_LOG = join(tmpdir(), 'claude-tgbot-spawn.log')

function rotate(path: string): void {
  try {
    if (statSync(path).size > 5 * 1024 * 1024) {
      const lines = readFileSync(path, 'utf8').split('\n')
      writeFileSync(path, lines.slice(Math.floor(lines.length / 2)).join('\n'))
    }
  } catch {}
}
function logChat(line: string): void {
  try {
    mkdirSync(LOG_DIR, { recursive: true })
    appendFileSync(CHAT_LOG, `[${new Date().toISOString().slice(0, 19)}] ${line}\n`)
    rotate(CHAT_LOG)
  } catch {}
}
function logStream(obj: unknown): void {
  try {
    mkdirSync(LOG_DIR, { recursive: true })
    appendFileSync(STREAM_LOG, JSON.stringify(obj) + '\n')
    rotate(STREAM_LOG)
  } catch {}
}
function logSpawn(msg: string): void {
  try { appendFileSync(SPAWN_LOG, `[${new Date().toISOString()}] [${BOT}] ${msg}\n`) } catch {}
  process.stderr.write(`worker-manager[${BOT}]: ${msg}\n`)
}

// H-15：本机 dispatcher HTTP 接口需口令（dispatcher 启动时写 <CHANNEL_DIR>/.api-token）。
// 本进程内的内部出站（/send 死信告警等）读它并带上；读不到 → 记一行日志并跳过该次调用，
// 绝不无鉴权硬发，也不重试。
function dispatcherApiToken(): string | null {
  try { return readFileSync(join(CHANNEL_DIR, '.api-token'), 'utf8').trim() || null } catch { return null }
}

// H2b：死信告警给用户发一条可见提示（只告知"处理失败、已搁置"，绝不带消息正文）。
// 默认实现 = POST 本机 dispatcher 的 /send（与 worker-plugin 出站同源）。失败只记事件、不抛出。
async function sendUserMessage(chatId: string, text: string): Promise<void> {
  if (!chatId) return
  const token = dispatcherApiToken()
  if (!token) { logSpawn('wm_notify_skipped api_token_missing'); return }
  try {
    const r = await fetch(`${DISPATCHER_URL}/send`, {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-api-token': token },
      body: JSON.stringify({ chat_id: chatId, text }),
    })
    if (!r.ok) logSpawn(`wm_notify_failed status=${r.status}`)
  } catch (e) {
    logSpawn(`wm_notify_failed reason=${(e as Error)?.name ?? 'unknown'}`)
  }
}

// ─── 时间感知：每条入站消息带本地时刻，久别重逢再加一行间隔 ──────────────
// 进程内变量不够用——要解决的正是"worker 被回收后隔了十几小时"，重启即失忆就永远算不出
// 间隔。落一个小状态文件，tmp+rename 原子写，启动时读一次进内存。
// desync ③b：间隔按"本 bot 收到的最近一条真人消息"统一计时（保留键 _human 等，更新规则在
// chat_guard）；按 chat 分键的旧值照写，只为格式向后兼容。worker-plugin 读同一文件做跨聊天拦截。
const LAST_TS_FILE = join(CHANNEL_DIR, '.last-inbound-ts.json')
const lastInboundTs: Record<string, unknown> = (() => {
  try {
    const d = JSON.parse(readFileSync(LAST_TS_FILE, 'utf8'))
    return d && typeof d === 'object' && !Array.isArray(d) ? d as Record<string, unknown> : {}
  } catch { return {} }
})()
let inboundState = loadInboundState(lastInboundTs)
function rememberInboundTs(chatId: string, tsMs: number): void {
  if (!chatId || !Number.isFinite(tsMs)) return
  lastInboundTs[chatId] = tsMs
  Object.assign(lastInboundTs, inboundState)   // 四个保留键与按 chat 旧键同一次原子写
  try {
    mkdirSync(CHANNEL_DIR, { recursive: true })
    writeFileSync(`${LAST_TS_FILE}.tmp`, JSON.stringify(lastInboundTs))
    renameSync(`${LAST_TS_FILE}.tmp`, LAST_TS_FILE)
  } catch {}
}

// ─── spawn 前置处理（spawn-worker.sh 逻辑 TS 化）──────────────────────

// /clear 残桩：jsonl 存在但无 user/assistant 消息 → 删掉，避免 --resume 报 No conversation found
function cleanClearResidue(jsonlPath: string): void {
  try {
    const txt = readFileSync(jsonlPath, 'utf8')
    if (!/"type":"(user|assistant)"/.test(txt)) {
      rmSync(jsonlPath, { force: true })
      logSpawn(`删除无对话残桩 session(可能 /clear 后) @ ${jsonlPath}`)
    }
  } catch {}
}

// 凭证指纹：darwin 走 keychain（保留原兜底），其他平台读 ~/.claude/.credentials.json
function credentialFingerprint(): string {
  try {
    let raw = ''
    if (platform() === 'darwin') {
      const r = spawnSync('/usr/bin/security',
        ['find-generic-password', '-s', 'Claude Code-credentials', '-w'], { encoding: 'utf8' })
      raw = r.status === 0 ? r.stdout : ''
    }
    if (!raw) {
      const p = join(homedir(), '.claude', '.credentials.json')
      if (existsSync(p)) raw = readFileSync(p, 'utf8')
    }
    if (!raw) return ''
    const tok = (JSON.parse(raw).claudeAiOauth || {}).accessToken || ''
    return tok ? createHash('sha256').update(tok).digest('hex').slice(0, 16) : ''
  } catch { return '' }
}

function settingsEnv(): Record<string, string> {
  try {
    // 路径必须与 provider_watch 监听的那份一致，否则「盯 A 文件、起进程读 B 文件」
    const d = JSON.parse(readFileSync(settingsPath(), 'utf8'))
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(d.env || {})) {
      if (/^(ANTHROPIC_|CLAUDE_CODE_|DISABLE_|ENABLE_)/.test(k)) out[k] = String(v)
    }
    return out
  } catch { return {} }
}

function providerFingerprint(): string {
  const e = settingsEnv()
  return createHash('sha256')
    .update(`${e.ANTHROPIC_BASE_URL || ''}|${e.ANTHROPIC_MODEL || ''}`)
    .digest('hex').slice(0, 16)
}

// ─── H-26：启动前置段超时与缓存 ───────────────────────────────────────
/** 三处同步前置子进程共用的超时上界（单一常量，r1 审查建议 9）。 */
export const PRECHECK_SUBPROC_TIMEOUT_MS = 5000
/** 会话文件超过它就改流式读写（默认 8MiB）。 */
export const SESSION_STREAM_THRESHOLD_BYTES = 8 * 1024 * 1024

// CLI 版本缓存：键 = 二进制 mtime + size（§5b：不必读整文件哈希——那比直接读版本更慢）。
const __cliVersionCache = new Map<string, { key: string; version: string }>()

// CLI 版本指纹（.clivfp）。Windows .cmd 同样经 win_cmd.ts 起（worker / 摘要 / 这里，三处 cmd 消费者同源）：
// 裸 `cmd /c <path> --version` 在路径含 ( ) & ^ 时命令名被截断 → stdout 空 → checkFingerprint 对空值直接 false
// → CLI 升级永远测不到、不 strip thinking → resume 400；缺 /d 时 AutoRun（clink 等）横幅还会混进首行成为指纹。
// run 可注入，白盒测试用（worker_manager_exit.test.ts）。
export function cliVersion(claudeBin: string, viaCmd: boolean, run: typeof spawnSync = spawnSync): string {
  // H-26 §5b/§6：同 stat（mtime+size）命中进程内缓存 → 不再起子进程；改 mtime 或 size 即失效。
  let cacheKey = ''
  try { const st = statSync(claudeBin); cacheKey = `${st.mtimeMs}:${st.size}` } catch { cacheKey = '' }
  if (cacheKey) {
    const hit = __cliVersionCache.get(claudeBin)
    if (hit && hit.key === cacheKey) return hit.version
  }
  let r: { stdout?: string | Buffer | null }
  if (viaCmd) {
    let spec
    // 路径被 win_cmd.ts 的 %…%/!…! 守卫拒绝：指纹留空（checkFingerprint 对空值不写），紧接着 spawnWorker 会用同一原因 fail-loud
    try { spec = winCmdSpawnSpec(claudeBin, ['--version'], { encoding: 'utf8' }) } catch { return '' }
    r = run(spec.file, spec.args, spec.opts as any)
  } else {
    r = run(claudeBin, ['--version'], { encoding: 'utf8' })
  }
  const version = String(r.stdout || '').split('\n')[0].trim()
  if (cacheKey) __cliVersionCache.set(claudeBin, { key: cacheKey, version })
  return version
}

// sidecar 指纹比对（.tokenfp/.providerfp/.clivfp 文件名与旧架构一致）：变了 → strip 全部 thinking
function checkFingerprint(jsonlPath: string, suffix: string, cur: string, label: string): boolean {
  if (!cur) return false
  const fpFile = `${jsonlPath}.${suffix}`
  let prev = ''
  try { prev = readFileSync(fpFile, 'utf8').trim() } catch {}
  try { writeFileSync(fpFile, cur) } catch {}
  if (prev && prev !== cur) { logSpawn(`${label} 变化 → strip 全部 thinking`); return true }
  return false
}

// thinking 剥离（防 resume 400 Invalid signature）。
// H-01：不再整行删除（删行会让 CLI 后续行 parentUuid 悬空 → 会话链断）。
// 改为保行、只把 thinking 块换成占位文本块；行的 uuid/parentUuid/顺序一律不动。
const OLD_THINKING_PLACEHOLDER = '[旧思考已省略]'
// strip 模式：'12h' 只动 12 小时前的旧 thinking；'all' 全清。布尔入参兼容（true=all）。
type StripMode = '12h' | 'all' | boolean

/** 同目录临时文件 + 0600 + 权限对齐原文件 + rename 原子替换。任何失败抛错（调用方保证原文件不变）。 */
function writeFileAtomic(path: string, data: string): void {
  let mode = 0o600
  try { mode = statSync(path).mode & 0o777 } catch {}
  const tmp = join(dirname(path), `.${basename(path)}.tmp-${process.pid}-${Date.now()}`)
  try {
    writeFileSync(tmp, data, { mode: 0o600 })
    try { chmodSync(tmp, mode) } catch {}
    renameSync(tmp, path)
  } catch (e) {
    try { unlinkSync(tmp) } catch {}
    throw e
  }
}

// H-10：备份文件名集合（同目录、同后缀）。用"集合差集"判定本次是否真的生成了新备份，
// 不靠 mtime/后缀单向判断（r1 审查重要 4）。
function backupFileSet(jsonlPath: string, suffix: string): Set<string> {
  const out = new Set<string>()
  const prefix = basename(jsonlPath) + '.bak.'
  try {
    for (const n of readdirSync(dirname(jsonlPath))) {
      if (n.startsWith(prefix) && n.endsWith('.' + suffix)) out.add(n)
    }
  } catch { /* 目录不可读 → 空集（视为无新备份） */ }
  return out
}

/** H-10：调 backupSessionJsonl 前后对比备份文件名集合；确有新文件才 true（否则本次不改写）。 */
function backupGrew(jsonlPath: string, suffix: 'strip' | 'compact'): boolean {
  const before = backupFileSet(jsonlPath, suffix)
  backupSessionJsonl(jsonlPath, suffix)
  const after = backupFileSet(jsonlPath, suffix)
  for (const n of after) if (!before.has(n)) return true
  return false
}

// ─── 剥离思考块 ─────────────────────────────────────────────────────
export function stripThinking(jsonlPath: string, mode: StripMode): { ok: boolean; replaced: number; skippedBadLines: number; error?: string } {
  const stripAll = mode === true || mode === 'all'
  let txt: string
  try { txt = readFileSync(jsonlPath, 'utf8') } catch { return { ok: true, replaced: 0, skippedBadLines: 0 } }
  if (!txt.includes('"type":"thinking"')) return { ok: true, replaced: 0, skippedBadLines: 0 }
  const cutoff = Date.now() / 1000 - 12 * 3600
  const out: string[] = []
  let replaced = 0, skippedBadLines = 0
  for (const line of txt.split('\n')) {
    if (!line.trim()) { out.push(line); continue }
    let o: any
    try { o = JSON.parse(line) } catch { skippedBadLines++; out.push(line); continue }
    const c = (o.message || {}).content
    if (!Array.isArray(c) || !c.some((x: any) => x?.type === 'thinking')) { out.push(line); continue }
    let ts = 0
    try { ts = new Date(String(o.timestamp || '').replace('Z', '+00:00')).getTime() / 1000 || 0 } catch {}
    if (!stripAll && !(ts && ts < cutoff)) { out.push(line); continue } // 缺/坏时间戳或近期行 → 不动
    // 占位块放第一个 thinking 块的原位置；mixed 行删 thinking、其余保序；删空才插占位
    const nc: any[] = []
    let inserted = false
    for (const b of c) {
      if (b && typeof b === 'object' && b.type === 'thinking') {
        if (!inserted) { nc.push({ type: 'text', text: OLD_THINKING_PLACEHOLDER }); inserted = true }
      } else { nc.push(b) }
    }
    if (!inserted) nc.push({ type: 'text', text: OLD_THINKING_PLACEHOLDER }) // 防御：不该发生
    o.message.content = nc
    out.push(JSON.stringify(o))
    replaced++
  }
  if (replaced === 0) return { ok: true, replaced: 0, skippedBadLines }
  if (!backupGrew(jsonlPath, 'strip')) {   // H-10：备份建不出来 → 跳过改写，绝不无备份覆盖
    logSpawn('wm_backup_failed_skip_write')
    return { ok: false, replaced: 0, skippedBadLines, error: 'backup_failed' }
  }
  try {
    writeFileAtomic(jsonlPath, out.join('\n'))
  } catch (e) {
    return { ok: false, replaced: 0, skippedBadLines, error: String((e as Error)?.message || e) }
  }
  logSpawn(`stripped thinking in ${replaced} lines (${stripAll ? 'all' : 'older than 12h'})`)
  return { ok: true, replaced, skippedBadLines }
}

// ─── H-02 会话压缩预算常量（集中在此，便于统一调整）─────────────────────
// 图片/二进制块按固定值估：base64 正文的长度不代表真实 token（图片被算成 6.6 万 token 是旧 bug）。
const CTX_TOKEN_EST_IMAGE = 2_000    // 含 base64 的 image 块
const CTX_TOKEN_EST_BINARY = 500     // 含 base64 的其它二进制块（tool_result/document 等）
const CTX_CHARS_PER_TOKEN = 3        // 纯文本粗算：字符数 ÷ 3
const COMPACT_TRIGGER = 170_000      // 估算超过它才压缩
const COMPACT_BUDGET = 100_000       // 压缩后保留区目标预算
const COMPACT_MIN_DIALOG = 8         // 无论如何至少保留的最近对话行数

/** 块内是否含 base64 正文（决定用固定估值还是字符粗算）。 */
function blockHasBase64(b: any): boolean {
  if (!b || typeof b !== 'object') return false
  const src = b.source
  if (src && typeof src === 'object') {
    if (src.type === 'base64' && typeof src.data === 'string') return true
    if (typeof src.data === 'string' && src.data.length > 128) return true
  }
  if (typeof b.data === 'string' && b.data.length > 128) return true
  const c = b.content
  if (Array.isArray(c)) { for (const x of c) if (blockHasBase64(x)) return true }
  else if (typeof c === 'string' && c.length > 256 && /^[A-Za-z0-9+/=\s]+$/.test(c)) return true
  return false
}

/** 单个 content 块的 token 估算。 */
function estimateBlock(b: any): number {
  if (b && typeof b === 'object' && blockHasBase64(b)) {
    return b.type === 'image' ? CTX_TOKEN_EST_IMAGE : CTX_TOKEN_EST_BINARY
  }
  let s = ''
  try { s = JSON.stringify(b ?? null) || '' } catch { return 0 }
  return Math.floor(s.length / CTX_CHARS_PER_TOKEN)
}

// 尺寸守卫：估算只覆盖最后一个 compact_boundary 之后的行（更早的行早已不在 CLI 上下文里，
// 只作计数下限、不删除）；超预算则丢最老对话行（其余行保序保留）。纯文件操作不调 LLM。老对话在 .bak。
export function compactSessionIfHuge(jsonlPath: string): { ok: boolean; kept: number; dropped: number; estimated: number; error?: string } {
  let lines: string[]
  try { lines = readFileSync(jsonlPath, 'utf8').split('\n') } catch { return { ok: true, kept: 0, dropped: 0, estimated: 0 } }
  const parsed: (any | null)[] = lines.map(l => { if (!l.trim()) return null; try { return JSON.parse(l) } catch { return null } })
  let lastBoundary = -1
  for (let i = 0; i < parsed.length; i++) { const o = parsed[i]; if (o && o.subtype === 'compact_boundary') lastBoundary = i }

  const sizeOfIdx = (i: number): number => {
    const o = parsed[i]
    if (!o || (o.type !== 'user' && o.type !== 'assistant')) return 0
    const c = (o.message || {}).content
    if (!Array.isArray(c)) return 0
    return c.reduce((s: number, b: any) => s + estimateBlock(b), 0)
  }
  // 边界之后的对话行（估算与压缩都只看这些）
  const dialogIdx: number[] = []
  for (let i = lastBoundary + 1; i < lines.length; i++) {
    const o = parsed[i]
    if (o && (o.type === 'user' || o.type === 'assistant') && Array.isArray((o.message || {}).content)) dialogIdx.push(i)
  }
  const total = dialogIdx.reduce((s, i) => s + sizeOfIdx(i), 0)
  if (dialogIdx.length === 0 || total <= COMPACT_TRIGGER) return { ok: true, kept: 0, dropped: 0, estimated: total }
  logSpawn(`session ctx ~${total} tokens > ${COMPACT_TRIGGER}，结构化压缩…`)

  // 从最新往回给对话行配额：放不下的行跳过（不 break），至少保留 COMPACT_MIN_DIALOG 行
  let budget = COMPACT_BUDGET
  const keep = new Set<number>()
  for (let k = dialogIdx.length - 1; k >= 0; k--) {
    const i = dialogIdx[k], sz = sizeOfIdx(i)
    if (keep.size < COMPACT_MIN_DIALOG) { keep.add(i); if (budget - sz >= 0) budget -= sz; continue }
    if (budget - sz < 0) continue // 放不下 → 跳过该行继续往前
    budget -= sz; keep.add(i)
  }
  if (keep.size === 0) { keep.add(dialogIdx[dialogIdx.length - 1]); logSpawn('compact: 极端情形，仅保最新 1 行') }

  // 保留区第一行对齐到真人 user 消息（排除 tool_result 行）；找不到则整轮不压缩
  const isRealUser = (o: any): boolean =>
    (o?.message?.role === 'user' || o?.type === 'user') &&
    Array.isArray(o.message?.content) && !o.message.content.some((b: any) => b?.type === 'tool_result')
  let firstKept = Math.min(...keep)
  let anchor = -1
  for (let i = firstKept; i > lastBoundary; i--) { if (parsed[i] && isRealUser(parsed[i])) { anchor = i; break } }
  if (anchor === -1) { logSpawn('compact: 边界后找不到真人 user 行，放弃压缩'); return { ok: true, kept: 0, dropped: 0, estimated: total } }
  keep.add(anchor)
  firstKept = Math.min(...keep)

  const dialogSet = new Set(dialogIdx)
  const outLines: string[] = []
  for (let i = 0; i < lines.length; i++) {
    if (i > lastBoundary && dialogSet.has(i) && !keep.has(i)) continue // 丢弃预算外的老对话行
    if (i === firstKept && parsed[i]) { parsed[i].parentUuid = null; outLines.push(JSON.stringify(parsed[i])) }
    else outLines.push(lines[i])
  }
  if (keep.size < 1) return { ok: true, kept: 0, dropped: 0, estimated: total } // 保底：压不出对话行则不写盘
  const dropped = dialogIdx.length - keep.size
  if (!backupGrew(jsonlPath, 'compact')) {   // H-10：备份建不出来 → 跳过改写
    logSpawn('wm_backup_failed_skip_write')
    return { ok: false, kept: 0, dropped: 0, estimated: total, error: 'backup_failed' }
  }
  try {
    writeFileAtomic(jsonlPath, outLines.join('\n'))
  } catch (e) {
    return { ok: false, kept: 0, dropped: 0, estimated: total, error: String((e as Error)?.message || e) }
  }
  logSpawn(`compacted session: kept ${keep.size} dialog lines, dropped ${dropped}`)
  return { ok: true, kept: keep.size, dropped, estimated: total }
}

// ─── worker 子进程环境白名单（H-11）──────────────────────────────────
// worker 是 --dangerously-skip-permissions 的模型进程；它一旦被诱导执行 `env`
// 就能把 dispatcher 的全部环境（含 TELEGRAM_BOT_TOKEN / PEERS_JSON）读走。
// 故改为白名单：只传下面明确列出的键，其余（含任何未列出的键）一律不传。
//
// 必填（缺一即 workerEnv 抛错，日志只打键名、不打值）：
//   PATH .................... 找 claude / bun 等可执行文件
//   HOME .................... claude 读 ~/.claude 配置与钥匙串凭证
//   CHANNEL_DIR ............. worker 读写 inbox / access.json / sidecar
//   TELEGRAM_WORKER_CHAT_ID . 默认聊天 id（worker 侧来源标识）
//   TELEGRAM_WORKER_BOT ..... 本 bot 名（worker-plugin 用）
//   TELEGRAM_DISPATCHER_URL . worker 出站回 dispatcher 的地址
//   ANTHROPIC_BASE_URL ...... 模型网关地址（worker 调模型用）
//   ANTHROPIC_AUTH_TOKEN .... 本 bot 自己的模型凭证（不是 Telegram 令牌）
// 可选（源里有才传）：见 OPTIONAL_WORKER_ENV。
// 绝不传：TELEGRAM_BOT_TOKEN、PEERS_JSON、ANTHROPIC_API_KEY，以及任何未列出的键。
export const REQUIRED_WORKER_ENV = [
  'PATH', 'HOME', 'CHANNEL_DIR', 'TELEGRAM_WORKER_CHAT_ID', 'TELEGRAM_WORKER_BOT',
  'TELEGRAM_DISPATCHER_URL', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN',
] as const
export const OPTIONAL_WORKER_ENV = [
  'LANG', 'LC_ALL', 'TERM', 'TMPDIR', 'USER', 'SHELL', 'NO_PROXY', 'no_proxy',
  'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
  // 仓库根（dispatcher/ 的上一级）：comfyui-skill 与压缩钩子靠它定位 scripts/。非凭证，随源传递。
  'CLAUDEBOTLIFE_REPO',
] as const
// ANTHROPIC_DEFAULT_*_MODEL 家族按通配放行（如 ANTHROPIC_DEFAULT_OPUS_MODEL）
const WORKER_ENV_OPTIONAL_RE = /^ANTHROPIC_DEFAULT_[A-Z]+_MODEL$/

function isWhitelistedWorkerKey(k: string): boolean {
  return (REQUIRED_WORKER_ENV as readonly string[]).includes(k)
    || (OPTIONAL_WORKER_ENV as readonly string[]).includes(k)
    || WORKER_ENV_OPTIONAL_RE.test(k)
}

/**
 * 按白名单过滤 dispatcher 环境，产出 worker 子进程环境（H-11）。
 * `required` 里每个键必须在 source 中出现且非空，否则抛错（错误信息只含键名）。
 */
export function workerEnv(
  source: Record<string, string | undefined>,
  required: readonly string[] = REQUIRED_WORKER_ENV,
): Record<string, string> {
  const out: Record<string, string> = {}
  for (const k of Object.keys(source)) {
    const v = source[k]
    if (typeof v === 'string' && v.length > 0 && isWhitelistedWorkerKey(k)) out[k] = v
  }
  const missing = required.filter(k => !(typeof source[k] === 'string' && (source[k] as string).length > 0))
  if (missing.length > 0) throw new Error(`worker env 缺少必填变量: ${missing.join(', ')}`)
  return out
}

// 生产 worker 启动不强制 ANTHROPIC_*：官方订阅模式下 settings.json 不写它们，
// 由 claude 自己读平台凭证（见 buildEnv 末尾注释）。其余身份变量必须有。
const PRODUCTION_REQUIRED_WORKER_ENV = [
  'PATH', 'HOME', 'CHANNEL_DIR', 'TELEGRAM_WORKER_BOT', 'TELEGRAM_DISPATCHER_URL',
] as const

// worker 子进程 env：清旧 ccp 残留 → 叠 settings.json env → 代理重定向 → 身份变量 → 白名单过滤
function buildEnv(): Record<string, string> {
  const env: Record<string, string | undefined> = { ...process.env }
  for (const k of ['ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
    'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL']) {
    delete env[k]
  }
  Object.assign(env, settingsEnv())
  // GUI 私有回环代理 → 常驻 daemon（8789→8799 / 8788→8798），没配就是 no-op
  for (const k of Object.keys(env)) {
    const v = env[k]
    if (typeof v === 'string' && v.includes('127.0.0.1:87')) {
      env[k] = v.replace('127.0.0.1:8789', '127.0.0.1:8799').replace('127.0.0.1:8788', '127.0.0.1:8798')
    }
  }
  env.CHANNEL_DIR = CHANNEL_DIR
  env.TELEGRAM_WORKER_BOT = BOT
  env.TELEGRAM_DISPATCHER_URL = DISPATCHER_URL
  // 仓库根（dispatcher/ 的上一级）→ comfyui-skill 靠它定位 scripts/comfyui_gen.py 和 configs/
  env.CLAUDEBOTLIFE_REPO = join(import.meta.dir, '..')
  // 订阅模式不注入静态 token：claude 自己读平台凭证并自动续期（darwin=keychain, win=DPAPI）
  return workerEnv(env, PRODUCTION_REQUIRED_WORKER_ENV)
}

// worker 的 MCP 配置由 manager 自己写（消灭对部署机 ~/.claude/dispatcher/worker-mcp.json 的隐式依赖）
function writeMcpConfig(): string {
  const p = join(CHANNEL_DIR, 'worker-mcp.json')
  const pluginPath = join(import.meta.dir, 'worker-plugin.ts')
  writeFileSync(p, JSON.stringify({
    mcpServers: {
      'telegram-worker': {
        command: 'bun',
        args: [pluginPath],
        env: { TELEGRAM_WORKER_BOT: BOT, TELEGRAM_DISPATCHER_URL: DISPATCHER_URL, CHANNEL_DIR },
      },
    },
  }, null, 2))
  return p
}

// ─── 凑一波再递（进线防抖合并）────────────────────────────────────────
// 用户连发 N 条时，第一条一进 stdin 就开了回合 → claude 只看到第一条，其余挤进下一轮。
// 改成：空闲时首条先不入队，开 burstWindowMs 窗口收同 chat 的后续消息，静默满窗（或自
// 首条起满 burstMaxMs）再合并成一条入队。媒体 / 内部合成消息 / 群里换人都不并（截波单投）。
// burstWindowMs=0 → collector.enabled=false，入队路径回归现状。
const BURST_DIR = join(CHANNEL_DIR, '.burst')
const BURST_TICK_MS = 500

// ─── 队列条目 ──────────────────────────────────────────────────────────
type QueueItem =
  | { kind: 'file'; path: string }
  | { kind: 'raw'; content: string; label: string }   // slash / /inject 调试注入

// ─── WorkerManager ────────────────────────────────────────────────────
const BACKOFF_MS = [1_000, 5_000, 30_000, 60_000]
const STOP_GRACE_MS = 5_000                 // SIGTERM 后等退出的宽限；未退 → 补 SIGKILL
const STOP_TIMEOUT_MS = 15_000              // 仍无 exit → 强制落地 stopped（迟到 exit 吞掉）

// ─── H2b 在飞消息重放常量（集中在此，便于统一调整）──────────────────────
const STALL_MS = 5 * 60_000                 // 距"最近一次 worker 事件"超它 → 判卡死（不是按写入时刻）
const MAX_REPLAY = 2                        // 已重放次数上限；到顶仍收可重试错误 → 死信
const INFLIGHT_PREFIX = '.inflight-'        // 在飞件名前缀
const PENDING_SUBDIR = '.pending'           // 待投 / 在飞的暂存子目录（点开头，天然躲开 *.json 扫描）
const DEAD_LETTER_SUBDIR = '.dead-letter'   // 死信目录
// is_error 文本兜底：fatal 标记集（收窄；泛词如 not found 不算）；词边界匹配避免命中毫秒数
const FATAL_TEXT_MARKERS = ['invalid api key', 'authentication_error', 'model not found', 'permission_error']
const RETRYABLE_TEXT_MARKERS = ['econnreset', 'etimedout', 'socket hang up', 'overloaded', 'timeout']

/** 在飞件文件名：`<stem>.part` 永不以 .json 结尾（挡不住点开头、但挡得住按 *.json 扫的既有逻辑）。 */
function inflightFileName(attempt: number, stem: string): string {
  return `${INFLIGHT_PREFIX}${attempt}-${stem}.part`
}

/** 解析在飞件名 → { attempt, stem }；非在飞名返回 null。 */
export function parseInflightName(name: string): { attempt: number; stem: string } | null {
  if (!name.startsWith(INFLIGHT_PREFIX)) return null
  const rest = name.slice(INFLIGHT_PREFIX.length)
  const dash = rest.indexOf('-')
  if (dash < 0) return null
  const attempt = Number(rest.slice(0, dash))
  if (!Number.isInteger(attempt) || attempt < 0) return null
  const stem = rest.slice(dash + 1).replace(/\.part$/i, '')
  return { attempt, stem }
}

/** 从任意待投 / 在飞文件名取"原始 stem"（去掉 .json / 在飞前缀与 .part 后缀）。 */
function pendingStem(name: string): string {
  const infl = parseInflightName(name)
  if (infl) return infl.stem
  return name.replace(/\.json$/i, '')
}

/** 源文件所在目录 → 它的 `.pending` 暂存目录（同目录内移动，不跨盘）。 */
function flightDirFor(srcPath: string): string {
  const dir = dirname(srcPath)
  return basename(dir) === PENDING_SUBDIR ? dir : join(dir, PENDING_SUBDIR)
}

/** 源文件 → 该 attempt 的在飞路径。 */
function inflightPathFor(srcPath: string, attempt: number): string {
  return join(flightDirFor(srcPath), inflightFileName(attempt, pendingStem(basename(srcPath))))
}

/** 排序键：取原名里的时间前缀（前导数字）；无数字则退化为字符串比较。 */
function timeSortKey(name: string): string {
  const stem = pendingStem(name)
  const m = stem.match(/^(\d+)/)
  return m ? m[1]!.padStart(20, '0') : stem
}

/** 结果事件 is_error 分类：先结构化字段，再文本兜底（需 HTTP/status 上下文），判不出 → retryable。 */
export function classifyResultError(ev: any): 'fatal' | 'retryable' {
  const nums: number[] = []
  for (const v of [ev?.status, ev?.http_status, ev?.error?.status]) {
    const n = typeof v === 'number' ? v : (typeof v === 'string' && /^\d+$/.test(v.trim()) ? Number(v) : NaN)
    if (Number.isFinite(n)) nums.push(n)
  }
  for (const n of nums) {
    if (n === 429 || (n >= 500 && n <= 599)) return 'retryable'
    if (n >= 400 && n <= 499) return 'fatal'
  }
  const type = typeof ev?.error?.type === 'string' ? ev.error.type.toLowerCase() : ''
  if (type) {
    if (FATAL_TEXT_MARKERS.some(m => type.includes(m))) return 'fatal'
    if (type.includes('rate') || RETRYABLE_TEXT_MARKERS.some(m => type.includes(m))) return 'retryable'
  }
  const text = [ev?.message, ev?.text, ev?.error?.message,
    typeof ev?.content === 'string' ? ev.content : ''].filter((s: unknown) => typeof s === 'string').join('\n').toLowerCase()
  if (text) {
    if (FATAL_TEXT_MARKERS.some(m => text.includes(m))) return 'fatal'
    if (/\b(http|status)\b[^\d]{0,8}4\d\d\b/.test(text)) return 'fatal' // 4xx 需 HTTP/status 上下文（挡毫秒数）
    if (/\b(http|status)\b[^\d]{0,8}(429|5\d\d)\b/.test(text)) return 'retryable'
    if (new RegExp(`\\b(${RETRYABLE_TEXT_MARKERS.join('|')})\\b`).test(text)) return 'retryable'
  }
  return 'retryable'
}

// 可注入依赖（INTERFACE §一）：测试注入假 spawn/kill/时钟/procInfo，绝不真起/真杀进程。
export interface WorkerDeps {
  channelDir?: string
  now?: () => number
  timer?: {
    setTimeout: (fn: () => void, ms?: number) => any
    clearTimeout: (id: any) => void
    setInterval: (fn: () => void, ms?: number) => any
    clearInterval: (id: any) => void
  }
  spawn?: (cmd: string, spec?: unknown) => any          // 返回带 pid/on/emit 的句柄
  kill?: (pid: number, force: boolean) => void
  procInfo?: (pid: number) => ProcInfo | null
  writeSession?: () => void                             // 会话回写钩子（抛错只记 wm_session_write_failed）
  send?: (chatId: string, text: string) => void          // 给用户发一条可见提示（死信告警；注入假件测）
}

// 冷轮（全新会话 / /clear 之后）无历史示范惯性，模型易直接吐文本忘调 reply → 用户收不到。
// 只在冷轮给下一条真人消息前置一次性提醒，不动沉默权（选择不回仍可不调 reply）。
const REPLY_REMINDER =
  '【系统提醒·仅本轮】你在 Telegram 上和用户对话。要让对方真正收到你的话，'
  + '必须调用 reply 工具发送——直接输出文本用户是看不到的。'
  + '（若你按当下人设选择不回复，那就不调 reply、直接结束本轮，这没问题。）'

export class WorkerManager {
  private proc: ChildProcessWithoutNullStreams | null = null
  // 状态机相位：stopped|starting|running|stopping。stopping 期间 ensure() 不再起新进程。
  private phaseVal: 'stopped' | 'starting' | 'running' | 'stopping' = 'stopped'
  private handledExit = false             // 每个进程实例只处理一次 exit（含超时强制停后的迟到 exit）
  private pendingRestart = false          // stopping 期间到点的退避意图，exit 后消费
  private stopRequested = false           // 主动 stop()：exit 后不自排退避重启
  private pid: number | null = null
  private stopWaitResolve: (() => void) | null = null
  private stopGraceTimer: any = null
  private stopTimeoutTimer: any = null
  // H-12 单一权威门：清空期间由 clearGate 同时管住"不得 spawn"（ensure 内先查），
  // 直到会话文件移走后才放开；clearExit* 用于等"旧进程 exit 事件"到达。
  private clearGate = false
  private clearExitPid: number | null = null
  private clearExitResolve: (() => void) | null = null
  private pumping = false                 // 异步组装（/photo_round）期间挡住重入
  private lastHuman: LastHumanMessage = null  // 最近一条投递的真人消息（承诺工具的作用聊天）
  private inFlight: QueueItem | null = null
  private inFlightSince = 0
  private inFlightContent = ''            // 在飞那条的组装结果：重启回插只能靠它（原件已落盘保留）
  private inFlightAttempt = 0             // 当前在飞的 attempt（每次重放 +1；用于挡迟到事件）
  private inFlightChatId = ''             // 当前在飞所属 chat（死信提示要发回它）
  private lastEventAt = 0                 // 最近一次 worker 事件（stdout 数据 / result）的时刻
  private queue: QueueItem[] = []
  private restartAttempt = 0
  private intentionalKill = false
  private needsReplyReminder = false      // 冷轮标记：下一条真人消息前置 REPLY_REMINDER
  private stdoutBuf = ''
  private watchers: FSWatcher[] = []
  private resultTimer: ReturnType<typeof setTimeout> | null = null
  private burst
  // 注入依赖（缺省 = 生产实现）
  private readonly channelDir: string
  private readonly now: () => number
  private readonly timer: WorkerDeps['timer'] & {}
  private readonly spawnFn: (cmd: string, spec?: unknown) => any
  private readonly killFn: (pid: number, force: boolean) => void
  private readonly procInfoFn: (pid: number) => ProcInfo | null
  private readonly writeSessionHook: (() => void) | null
  private readonly sendFn: (chatId: string, text: string) => void
  readonly sessionUuid: string

  constructor(deps: WorkerDeps = {}) {
    this.channelDir = deps.channelDir || CHANNEL_DIR
    this.now = deps.now || (() => Date.now())
    this.timer = deps.timer || { setTimeout, clearTimeout, setInterval, clearInterval }
    // 真 spawn 的 spec 由 spawnWorker 组装；注入的假 spawn 只关心第一参（命令描述串）。
    this.spawnFn = deps.spawn || ((_cmd, spec: any) => spawn(spec.file, spec.args, spec.opts))
    this.killFn = deps.kill || ((pid, force) => killTree(pid, force))
    this.procInfoFn = deps.procInfo || realProcInfo
    this.writeSessionHook = deps.writeSession || null
    this.sendFn = deps.send || ((chatId, text) => { void sendUserMessage(chatId, text) })
    this.sessionUuid = unifiedSessionUuid(BOT)
    const burstDir = join(this.channelDir, '.burst')
    mkdirSync(join(this.channelDir, 'inbox'), { recursive: true })
    mkdirSync(burstDir, { recursive: true })
    this.burst = createBurstCollector({
      ...readBurstCfg(this.channelDir),
      destDir: burstDir,
      deliver: p => this.enqueueFile(p),
      log: logSpawn,
    })
    // 崩在"合并件已落盘、还没入队"那一瞬 → 合并件留在 .burst，启动时收一次，绝不丢。
    // 只在这里收：.burst 刻意不进 inboxDirs，否则 drainInbox 每轮都会把合并件再收进
    // 一个新波次，白白多押 5 秒。
    try {
      for (const f of readdirSync(burstDir).sort()) {
        if (f.endsWith('.json')) this.queue.push({ kind: 'file', path: join(burstDir, f) })
      }
      // H2b：合并件被投递后改名进 .pending（含在飞件）；启动时一并收回，绝不丢。
      const burstPending = join(burstDir, PENDING_SUBDIR)
      for (const f of readdirSync(burstPending).sort()) {
        if (f.endsWith('.json') || f.startsWith(INFLIGHT_PREFIX)) this.queue.push({ kind: 'file', path: join(burstPending, f) })
      }
    } catch {}
    this.watchInboxes()
    this.timer.setInterval(() => this.drainInbox(), 5_000)  // Windows fs.watch 语义差异保险
    this.timer.setInterval(() => this.checkStuck(), 30_000)
    if (this.burst.enabled) this.timer.setInterval(() => this.burst.tick(), BURST_TICK_MS)
  }

  // 当前相位（INTERFACE §一导出面）
  phase(): 'stopped' | 'starting' | 'running' | 'stopping' { return this.phaseVal }

  // ── 对外接口 ─────────────────────────────────────────────────────
  isAlive(): boolean { return this.proc !== null && this.phaseVal !== 'stopped' }

  private pidFilePath(): string { return join(this.channelDir, '.worker.pid') }

  private removePidFile(): void { try { rmSync(this.pidFilePath(), { force: true }) } catch {} }

  async ensure(): Promise<void> {
    // H-12：清空门禁期间不得起新进程（判定在 spawn 之前，无 await 空档）。
    if (this.clearGate) { logSpawn('wm_spawn_skipped reason=clear_gate'); return }
    if (this.phaseVal === 'starting' || this.phaseVal === 'running') {
      logSpawn(`wm_spawn_skipped reason=${this.phaseVal}`)
      return
    }
    if (this.phaseVal === 'stopping') { logSpawn('wm_spawn_skipped reason=stopping'); return }
    await this.spawnWorker()
  }

  /** H-12：开启清空门禁（可观测 wm_clear_gate on）。此后 ensure() 不再起新 worker。 */
  beginClear(): void {
    this.clearGate = true
    logSpawn('wm_clear_gate on')
  }

  /** H-12：释放清空门禁（可观测 wm_clear_gate off），并唤醒：投递门禁期入队的消息。 */
  endClear(): void {
    this.clearGate = false
    logSpawn('wm_clear_gate off')
    void this.ensure().then(() => this.drainInbox())
  }

  /** H-12：杀掉当前 worker 并等其 exit 事件到达（上限 timeoutMs）。返回 'exited' | 'timeout' | 'not_running'。
   *  超时 → 记 wm_clear_exit_timeout 并强制落地（绝不永久持门）。 */
  async killAndWaitExit(timeoutMs: number): Promise<'exited' | 'timeout' | 'not_running'> {
    const pid = this.proc?.pid ?? null
    if (pid == null) return 'not_running'
    this.intentionalKill = true
    const wait = new Promise<void>(resolve => { this.clearExitPid = pid; this.clearExitResolve = resolve })
    this.killFn(pid, false)   // SIGTERM：让 claude 优雅收尾 jsonl（防写半行损坏）
    let timedOut = false
    await Promise.race([
      wait,
      new Promise<void>(r => { this.timer.setTimeout(() => { timedOut = true; r() }, timeoutMs) }),
    ])
    this.clearExitResolve = null
    this.clearExitPid = null
    if (!timedOut) return 'exited'
    logSpawn('wm_clear_exit_timeout')
    this.handledExit = true                 // 此后迟到的真 exit 被吞掉
    if (this.proc?.pid === pid) { this.proc = null; this.pid = null }
    this.phaseVal = 'stopped'
    this.removePidFile()
    return 'timeout'
  }

  sendSlash(text: string): void {
    this.queue.push({ kind: 'raw', content: text, label: `slash ${text.split(' ')[0]}` })
    // /clear 清空上下文 → 之后第一条真人消息是冷轮，易忘调 reply，补一次提醒
    if (/^\/clear\b/.test(text.trim())) this.needsReplyReminder = true
    void this.ensure().then(() => this.pump())
  }

  injectRaw(text: string): void {
    this.queue.push({ kind: 'raw', content: text, label: 'manual /inject' })
    void this.ensure().then(() => this.pump())
  }

  kill(opts: { intentional?: boolean } = {}): void {
    this.intentionalKill = opts.intentional === true
    // intentional(如 /clearall) 在 POSIX 走 SIGTERM 让 claude 优雅收尾 jsonl（防写半行损坏）；
    // Windows taskkill 恒 /F（不带对控制台进程无效）。this.proc.pid 确定是我们刚起的，无需校验。
    if (this.proc?.pid) this.killFn(this.proc.pid, !this.intentionalKill)
    this.removePidFile()                    // 进程已死，别留陈旧 pid
    this.phaseVal = 'stopped'
    this.proc = null
    this.pid = null
  }

  status(): Record<string, unknown> {
    return {
      bot: BOT, phase: this.phaseVal, session_uuid: this.sessionUuid,
      pid: this.proc?.pid ?? null, queue_depth: this.queue.length,
      in_flight: this.inFlight ? (this.inFlight.kind === 'file' ? this.inFlight.path : this.inFlight.label) : null,
    }
  }

  // 主动停止：SIGTERM → 等 exit；grace(5s) 未退 → SIGKILL；timeout(15s) 仍无 exit → 强制落地。
  // starting 期间先等 spawn 落地再杀（避免杀空）。stop() 返回时 phaseVal==='stopped'。
  async stop(force = false): Promise<void> {
    if (this.phaseVal === 'stopped') { this.removePidFile(); return }
    const enteredStarting = this.phaseVal === 'starting'
    if (enteredStarting) { await this.settleStarting() }
    if (this.phaseVal === 'stopped') { this.removePidFile(); return }   // spawn 期间失败
    this.stopRequested = true
    this.phaseVal = 'stopping'
    const pid = this.pid
    if (pid == null) { this.phaseVal = 'stopped'; this.removePidFile(); return }
    const wait = new Promise<void>(resolve => { this.stopWaitResolve = resolve })
    logSpawn(`wm_stop pid=${pid} force=${force}`)
    this.killFn(pid, force)                                             // force=false → SIGTERM
    if (!force) {
      this.stopGraceTimer = this.timer.setTimeout(() => {
        if (this.phaseVal === 'stopping' && this.pid === pid) this.killFn(pid, true)
      }, STOP_GRACE_MS)
    }
    this.stopTimeoutTimer = this.timer.setTimeout(() => {
      if (this.phaseVal !== 'stopping' || this.pid !== pid) return
      // 有 pendingRestart：保留重启意图，不吞 exit、不强制落地，交给 exit 后的自愈路径
      if (this.pendingRestart) return
      logSpawn('wm_stop_timeout')
      this.handledExit = true                                          // 此后迟到的真 exit 被吞掉
      this.phaseVal = 'stopped'
      this.proc = null
      this.pid = null
      this.removePidFile()
      this.finishStopWait()
    }, STOP_TIMEOUT_MS)
    // 起停衔接：stop 紧跟在 starting 之后时，spawn 刚落地、杀信号已发；不在原地等 exit
    // （exit/timeout 一到，onExit 或超时回调自会把状态落地并清 pid 文件）。
    if (enteredStarting) return
    await wait
    this.clearStopTimers()
  }

  // 等 spawn 落地（starting → running/stopped）：spawnWorker 在首个 await 前同步置 starting，
  // 故这里用微任务自旋即可推进（假件与真件同构）。
  private async settleStarting(): Promise<void> {
    let guard = 0
    while (this.phaseVal === 'starting' && guard++ < 10000) await Promise.resolve()
  }

  private clearStopTimers(): void {
    if (this.stopGraceTimer != null) { this.timer.clearTimeout(this.stopGraceTimer); this.stopGraceTimer = null }
    if (this.stopTimeoutTimer != null) { this.timer.clearTimeout(this.stopTimeoutTimer); this.stopTimeoutTimer = null }
  }

  private finishStopWait(): void {
    this.clearStopTimers()
    const r = this.stopWaitResolve
    this.stopWaitResolve = null
    if (r) r()
  }

  private scheduleBackoff(): void {
    const delay = BACKOFF_MS[Math.min(this.restartAttempt, BACKOFF_MS.length - 1)]
    this.restartAttempt++
    logSpawn(`${delay}ms 后自动 --resume 重启 (attempt ${this.restartAttempt})`)
    this.timer.setTimeout(() => this.onBackoff(), delay)
  }

  // 退避回调：只调 ensure()（stopped/starting/running 由 ensure 的核心不变式兜住），
  // 唯独 stopping 要保住"还要重启"的意图（pendingRestart），等 exit 后自愈。
  private onBackoff(): void {
    if (this.phaseVal === 'stopping') { this.pendingRestart = true; logSpawn('wm_restart_pending'); return }
    void this.ensure().then(() => this.drainInbox())
  }

  // ── spawn（spawn-worker.sh 等价物）───────────────────────────────
  private async spawnWorker(): Promise<void> {
    if (this.phaseVal === 'starting') return
    // 置位必须在任何 await 之前：同一 tick 内第二次 ensure() 看到 starting 即跳过（杜绝重入双起）
    this.phaseVal = 'starting'
    this.intentionalKill = false
    this.pendingRestart = false
    this.stopRequested = false
    this.handledExit = false

    // 会话回写（H-09 注入点）：失败只记事件、继续启动，绝不走上面的失败复位路径
    try { this.writeSessionHook?.() }
    catch { logSpawn('wm_session_write_failed') }

    const pidFile = this.pidFilePath()

    // 孤儿防护（H-03 身份校验）：只有四条件全真的陈旧 pid 才杀，否则一律留着（保守，宁漏勿误杀）
    try {
      let recorded: any = null
      try {
        const raw = readFileSync(pidFile, 'utf8').trim()
        if (raw) { try { recorded = JSON.parse(raw) } catch { recorded = raw } }
      } catch {}
      if (recorded != null) {
        const stalePid = (recorded && typeof recorded === 'object') ? Number(recorded.pid) : parseInt(String(recorded), 10)
        const info = Number.isFinite(stalePid) && stalePid > 0 ? this.procInfoFn(stalePid) : null
        if (shouldKillOrphan(stalePid, recorded, this.sessionUuid, info).kill) this.killFn(stalePid, true)
        this.removePidFile()   // 无论杀没杀，陈旧 pid 文件都清掉
      }
    } catch {}

    const botDir = this.channelDir
    const slug = projectSlug(botDir)
    const projDir = join(homedir(), '.claude', 'projects', slug)
    const jsonl = join(projDir, `${this.sessionUuid}.jsonl`)

    // 前置段整体包 try/catch：任何异常 → wm_start_failed step=<名>、复位 stopped、走退避（不卡 starting）
    let cmdDesc = ''
    let spec: any
    try {
      if (existsSync(jsonl)) cleanClearResidue(jsonl)
      const resume = existsSync(jsonl)
      this.needsReplyReminder = !resume       // 全新会话首轮属冷轮，补 reply 提醒
      const claude = resolveClaude()

      if (resume) {
        let stripAll = false
        stripAll = checkFingerprint(jsonl, 'tokenfp', credentialFingerprint(), '凭证 token') || stripAll
        stripAll = checkFingerprint(jsonl, 'providerfp', providerFingerprint(), 'provider(BASE_URL/MODEL)') || stripAll
        stripAll = checkFingerprint(jsonl, 'clivfp', cliVersion(claude.bin, claude.viaCmd), 'CLI 版本') || stripAll
        stripThinking(jsonl, stripAll)
        compactSessionIfHuge(jsonl)
      }

      const mcpConfig = writeMcpConfig()
      const args = [
        '-p',
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        ...(resume ? ['--resume', this.sessionUuid] : ['--session-id', this.sessionUuid]),
        '--dangerously-skip-permissions',
        '--setting-sources', 'user,project,local',
        '--add-dir', botDir,
        '--append-system-prompt-file', join(botDir, 'CLAUDE.md'),
        '--mcp-config', mcpConfig,
        '--strict-mcp-config',
      ]
      const env = buildEnv()
      // 保证子进程能找到 claude/bun（launchd/服务的精简 PATH 不含用户 bin 目录）
      const extraPath = platform() === 'darwin'
        ? [join(homedir(), '.local', 'bin'), '/opt/homebrew/bin', '/usr/local/bin']
        : [join(homedir(), '.bun', 'bin')]
      // Windows 环境变量键是 'Path'(大小写不敏感)；直接写 env.PATH 会造出 PATH/Path 双键，
      // 传给子进程哪个生效未定义 → 找不到 bun。找出现有键名(不区分大小写)覆盖它。
      const pathKey = Object.keys(env).find(k => k.toLowerCase() === 'path') || 'PATH'
      env[pathKey] = [...extraPath, env[pathKey] || ''].join(delimiter)

      cmdDesc = `spawn worker: ${resume ? '--resume' : '--session-id'} ${this.sessionUuid} (claude=${claude.bin})`
      // Windows .cmd：经 cmd /d /s /c 起，引号拼法只在 win_cmd.ts 一处（含空格路径/元字符/尾反斜杠都靠它）。
      spec = claude.viaCmd
        ? winCmdSpawnSpec(claude.bin, args, { cwd: botDir, env, stdio: ['pipe', 'pipe', 'pipe'] })
        : { file: claude.bin, args, opts: { cwd: botDir, env, stdio: ['pipe', 'pipe', 'pipe'] } }
    } catch (e) {
      logSpawn(`🔴 wm_start_failed step=prep：${(e as Error)?.message ?? e}`)
      this.phaseVal = 'stopped'
      this.scheduleBackoff()
      return
    }

    logSpawn(cmdDesc)
    let handle: any
    try {
      handle = this.spawnFn(cmdDesc, spec)
    } catch (e) {
      logSpawn(`🔴 wm_start_failed step=spawn：${(e as Error)?.message ?? e}`)
      this.phaseVal = 'stopped'
      this.scheduleBackoff()
      return
    }

    this.proc = handle as ChildProcessWithoutNullStreams
    this.pid = handle?.pid ?? null
    try {
      // 原子写：pid/启动时间/会话 uuid 三件套（孤儿清理的身份依据）
      writeFileSync(`${pidFile}.tmp`, JSON.stringify({ pid: this.pid, startedAtMs: this.now(), sessionUuid: this.sessionUuid }))
      renameSync(`${pidFile}.tmp`, pidFile)
    } catch {}
    this.wireProc(handle)

    // spawn 即 running：stdin 管道会缓冲，claude 启动完自然消费第一条消息。
    // （不能等 system/init——headless 下它在收到第一条输入后才发，等它=死锁）
    // await 一个微任务：让同一 tick 内并发的第二次 ensure() 仍观察到 'starting'（契约 §二.2）。
    await Promise.resolve()
    this.phaseVal = 'running'
    logSpawn(`wm_spawn pid=${this.pid}`)
    logSpawn('worker spawned, 管道就绪')
    this.pump()

    // slug 断言：spawn 后 claude 应在预期 projects 目录写 session；不匹配 = slugify 规则错 = 丢记忆
    this.timer.setTimeout(() => {
      if (this.phaseVal === 'running' && !existsSync(projDir)) {
        logSpawn(`🔴 slug 断言失败：预期 projects 目录不存在 ${projDir} — slugify 规则与 CLI 不一致，可能丢记忆！`)
      }
    }, 30_000)
  }

  // 绑定子进程事件。假件句柄没有 stdout/stderr/stdin → 一律可选链跳过（测试不真起进程）。
  private wireProc(handle: any): void {
    if (!handle || typeof handle.on !== 'function') return
    try { handle.stdout?.on?.('data', (chunk: Buffer) => this.onStdout(chunk)) } catch {}
    try {
      handle.stderr?.on?.('data', (chunk: Buffer) => {
        const s = chunk.toString().trim()
        if (s) logStream({ type: '_stderr', text: s.slice(0, 2000) })
      })
    } catch {}
    // 带上 proc 身份：onExit 要能认出「这条是上一个进程的遗言」（见 onExit 的陈旧退出守卫）
    handle.on('error', (err: any) => { logSpawn(`spawn error: ${err}`); this.onExit(-1, handle) })
    handle.on('exit', (code: number) => this.onExit(code ?? 0, handle))
  }

  // ── stdout 事件流 ─────────────────────────────────────────────────
  private onStdout(chunk: Buffer): void {
    this.lastEventAt = (this.now ?? Date.now)()   // 任何 stdout 数据都算"worker 还活着"，重置卡死计时
    this.stdoutBuf += chunk.toString()
    let nl: number
    while ((nl = this.stdoutBuf.indexOf('\n')) >= 0) {
      const line = this.stdoutBuf.slice(0, nl).trim()
      this.stdoutBuf = this.stdoutBuf.slice(nl + 1)
      if (!line) continue
      let ev: any
      try { ev = JSON.parse(line) } catch { continue }
      logStream(ev)
      this.onEvent(ev)
    }
  }

  private onEvent(ev: any): void {
    if (ev.type === 'system' && ev.subtype === 'init') {
      // ⚠️ init 在 headless 下是"收到第一条输入后"才发的（不是启动即发）——
      // 不能拿它当喂消息的前置门（会互相等死锁）。这里只作确认信号+归零退避。
      this.restartAttempt = 0
      logSpawn('worker init 确认 (system/init)')
      logChat('── worker 会话已确认 ──')
      return
    }
    if (ev.type === 'assistant') {
      const content = (ev.message || {}).content || []
      for (const c of content) {
        if (c?.type === 'text' && c.text?.trim()) logChat(`🤖 ${c.text.trim()}`)
        if (c?.type === 'tool_use') logChat(`⚙ 调用工具 ${c.name}(${JSON.stringify(c.input ?? {}).slice(0, 200)})`)
      }
      return
    }
    if (ev.type === 'system' && ev.subtype === 'compact_boundary') {
      logChat(`── 上下文已压缩 (${ev.compact_metadata?.trigger ?? '?'}) ──`)
      return
    }
    if (ev.type === 'result') {
      this.lastEventAt = (this.now ?? Date.now)()
      if (this.inFlight == null) { this.pump(); return }
      // 迟到事件：带 attempt 且与当前在飞不符 → 只记事件，不动文件与状态（防删除错件）
      const evAttempt = typeof ev.attempt === 'number' ? ev.attempt : null
      if (evAttempt != null && evAttempt !== this.inFlightAttempt) {
        logSpawn(`wm_stale_result attempt=${evAttempt} current=${this.inFlightAttempt}`)
        return
      }
      if (ev.is_error === true) {
        const kind = classifyResultError(ev)
        logSpawn(`wm_result_error kind=${kind}`)
        if (kind === 'fatal') this.deadLetterInFlight('fatal')
        else this.retryInFlight()
        return
      }
      this.settleInFlightSuccess()
      this.pump()
      return
    }
  }

  /** 该轮成功：删在飞件、清状态。 */
  private settleInFlightSuccess(): void {
    const item = this.inFlight
    if (item?.kind === 'file') { try { rmSync(item.path, { force: true }) } catch {} }
    this.inFlight = null
    this.inFlightContent = ''
    this.inFlightAttempt = 0
    this.inFlightChatId = ''
    if (this.resultTimer) { this.timer.clearTimeout(this.resultTimer); this.resultTimer = null }
  }

  /** 可重试失败（result / stall 共用）：停机中不重放（件留在 .pending，等下次启动扫描兜底）；
   *  到上限 → 死信；否则把原件改名成 attempt+1 的在飞件并原地重投。 */
  private retryInFlight(): void {
    const item = this.inFlight
    if (!item || item.kind !== 'file') {
      // raw 条目没有落盘原件：只能按内容回插（行为与旧版崩溃回插一致）
      if (item?.kind === 'raw') this.queue.unshift(item)
      else if (item && this.inFlightContent) this.queue.unshift({ kind: 'raw', content: this.inFlightContent, label: 'requeue-after-restart' })
      this.inFlight = null; this.inFlightContent = ''
      if (this.resultTimer) { this.timer.clearTimeout(this.resultTimer); this.resultTimer = null }
      return
    }
    if (this.phaseVal === 'stopping') {
      // 重放的 worker 刚起就被 stop 拆掉 = 悬挂；一律不重放，留件给下次启动扫描
      this.inFlight = null; this.inFlightContent = ''; this.inFlightAttempt = 0; this.inFlightChatId = ''
      if (this.resultTimer) { this.timer.clearTimeout(this.resultTimer); this.resultTimer = null }
      return
    }
    const cur = this.inFlightAttempt
    if (cur >= MAX_REPLAY) { this.deadLetterInFlight('replay_cap'); return }
    const next = cur + 1
    const dst = inflightPathFor(item.path, next)
    try { renameSync(item.path, dst) } catch { /* 改名失败照旧按原路径重投，绝不丢 */ }
    item.path = existsSync(dst) ? dst : item.path
    this.inFlightAttempt = next
    logSpawn(`wm_replay attempt=${next}`)
    const content = this.inFlightContent
    this.inFlight = null
    this.dispatchContent(item, content, this.inFlightChatId)
  }

  /** 移入死信目录 + 事件行 + 给用户一条可见提示（文案不含正文）；清在飞状态。 */
  private deadLetterInFlight(reason: string): void {
    const item = this.inFlight
    let chatId = this.inFlightChatId
    let stem = 'raw'
    if (item?.kind === 'file') {
      stem = pendingStem(basename(item.path))
      try {
        const m = JSON.parse(readFileSync(item.path, 'utf8'))
        if (m && typeof m === 'object' && m.chat_id != null) chatId = String(m.chat_id)
      } catch {}
      try {
        const pendDir = dirname(item.path)
        const rootDir = basename(pendDir) === PENDING_SUBDIR ? dirname(pendDir) : pendDir
        const deadDir = join(rootDir, DEAD_LETTER_SUBDIR)
        mkdirSync(deadDir, { recursive: true })
        renameSync(item.path, join(deadDir, `${stem}.json`))
      } catch {}
    }
    this.inFlight = null
    this.inFlightContent = ''
    this.inFlightAttempt = 0
    this.inFlightChatId = ''
    if (this.resultTimer) { this.timer.clearTimeout(this.resultTimer); this.resultTimer = null }
    logSpawn(`wm_dead_letter id=${stem} reason=${reason}`)
    try { this.sendFn(chatId, '有一条消息我处理时反复失败，已经先搁置了，稍后再帮你看看。') } catch {}
    this.pump()
  }

  private onExit(code: number, proc?: ChildProcessWithoutNullStreams): void {
    // H-12：清空流程在等这条 exit 事件——先唤醒（在一切早退之前，确保不因守卫而漏唤醒）。
    if (this.clearExitResolve && (proc?.pid ?? this.pid) === this.clearExitPid) {
      const r = this.clearExitResolve
      this.clearExitResolve = null
      this.clearExitPid = null
      r()
    }
    // 同一实例只处理一次：第二次到达 / 超时强制停后迟到的真 exit → 零副作用（吞掉）
    if (this.handledExit) return
    // H2b 迟到退出：带 attempt 且与当前在飞不符 → 只记事件，不动文件与状态（防上一轮 exit 清掉这一轮）
    const exitAttempt = typeof (proc as any)?.attempt === 'number' ? (proc as any).attempt : null
    if (exitAttempt != null && exitAttempt !== this.inFlightAttempt) {
      logSpawn(`wm_stale_exit attempt=${exitAttempt} current=${this.inFlightAttempt}`)
      return
    }
    // 陈旧退出守卫：kill() 到 exit 事件之间若有新消息触发 ensure() 起了新 worker，这条 exit 是
    // 上一个进程的。再往下走会把新 worker 的 proc/phase 清成 stopped 并多排一次 spawn ——
    // 两个 claude 写同一个 jsonl = 记忆损坏。provider 自动跟随把「主动 kill」变成常规操作，
    // 这个原本毫秒级没人踩到的窗口从此会被踩到。
    if (proc && this.proc && this.proc !== proc) {
      logSpawn(`忽略陈旧 worker 的 exit code=${code}（新 worker 已在跑）`)
      return
    }
    this.handledExit = true
    const exitPid = proc?.pid ?? this.pid ?? -1
    logSpawn(`wm_exit pid=${exitPid}`)
    logSpawn(`worker exit code=${code}${this.intentionalKill ? ' (intentional)' : ''}`)
    this.removePidFile()                    // 确认退出，pid 文件才作废
    this.proc = null
    this.pid = null
    this.phaseVal = 'stopped'
    const wasInFlight = this.inFlight
    const wasContent = this.inFlightContent
    this.inFlight = null
    this.inFlightContent = ''
    this.inFlightAttempt = 0
    this.inFlightChatId = ''
    if (this.resultTimer) { this.timer.clearTimeout(this.resultTimer); this.resultTimer = null }
    if (this.intentionalKill) { this.intentionalKill = false; this.finishStopWait(); return }
    // 在飞那条没跑完 → 重投队头。H2b：file 条目的原件已落盘保留（.pending/.inflight-*），由
    // drainInbox 兜底扫重新入队，不再拿内存内容按 raw 回插——否则同一件 raw + file 双投。
    // 仅当落盘件确实不在（老路径 / 内容只在内存里）才退回旧的 raw 回插。
    if (wasInFlight?.kind === 'raw') this.queue.unshift(wasInFlight)
    else if (wasInFlight && wasContent) {
      if (wasInFlight.kind !== 'file' || !existsSync(wasInFlight.path)) {
        this.queue.unshift({ kind: 'raw', content: wasContent, label: 'requeue-after-restart' })
      }
    }
    this.finishStopWait()                   // 唤醒 stop() 的等待
    // stopping 期间到点的退避意图：exit 后立刻自愈（不受下面的停止/退避分支影响）
    if (this.pendingRestart) {
      this.pendingRestart = false
      void this.ensure().then(() => this.drainInbox())
      return
    }
    // 主动 stop()：正常退出，不自排退避重启
    if (this.stopRequested) { this.stopRequested = false; return }
    this.scheduleBackoff()
  }

  private checkStuck(): void {
    if (!this.inFlight) return
    // H2b：判据改成"距最近一次 worker 事件（stdout 数据 / result）"，不再按写入时刻；
    // 长任务只要还在吐 stdout 就不算卡死，真静默满 STALL_MS 才按可重试处理（受 MAX_REPLAY 约束）。
    if ((this.now ?? Date.now)() - this.lastEventAt >= STALL_MS) {
      logSpawn(`wm_stall attempts=${this.inFlightAttempt}`)
      this.retryInFlight()
    }
  }

  // ── inbox → stdin ────────────────────────────────────────────────
  private inboxDirs(): string[] {
    const dirs = [join(CHANNEL_DIR, 'inbox')]
    try {
      const chats = join(CHANNEL_DIR, 'chats')
      if (existsSync(chats)) {
        for (const name of readdirSync(chats)) {
          const p = join(chats, name, 'inbox')
          if (existsSync(p)) dirs.push(p)
        }
      }
    } catch {}
    return dirs
  }

  private watchInboxes(): void {
    for (const w of this.watchers) { try { w.close() } catch {} }
    this.watchers = []
    for (const dir of this.inboxDirs()) {
      try {
        this.watchers.push(watch(dir, (_ev, fname) => {
          if (!fname || !fname.endsWith('.json')) return
          this.timer.setTimeout(() => this.drainInbox(), 20) // rename 完成缓冲
        }))
      } catch {}
    }
  }

  /** 攒波的前提 = 此刻入队就会立刻开回合（没有在飞轮次、也没有积压）。
   *  有积压时照旧排队——pump 本来就会把队里同 chat 的连发并成一条，行为不变。 */
  private burstIdle(): boolean {
    return !this.inFlight && this.queue.length === 0
  }

  /** 入队一个 inbox 原件/合并件（波次层 flush 的落点；tick 触发的 flush 不经过 drainInbox，
   *  所以这里自己推一次 pump）。已在队里/正在飞的直接跳过，绝不重复投。 */
  private enqueueFile(path: string): void {
    if (this.queue.some(q => q.kind === 'file' && q.path === path)) return
    if (this.inFlight?.kind === 'file' && this.inFlight.path === path) return
    this.queue.push({ kind: 'file', path })
    void this.ensure().then(() => this.pump())
  }

  private drainInbox(): void {
    for (const dir of this.inboxDirs()) {
      try {
        for (const f of readdirSync(dir).sort()) {
          if (!f.endsWith('.json')) continue
          const p = join(dir, f)
          // 已在攒波时也要走波次层：arrive() 认得出重复路径（fs.watch + 5s 兜底扫会重复报），
          // 走原路就会把还在波里的原件又入一次队 → 同一句话投两遍。
          if (this.burst.enabled && (this.burst.waves() > 0 || this.burstIdle())
              && this.burst.arrive(p)) continue
          this.enqueueFile(p)
        }
      } catch {}
    }
    // H2b：.pending 里是在飞件与已过波次的待投件。它们已"定稿"（不再合并），直接入队，
    // 且按**原文件名里的时间前缀**排序（不依赖目录枚举顺序，在飞名带 attempt 前缀会打乱字典序）。
    const staged: string[] = []
    for (const dir of this.inboxDirs()) {
      const pend = join(dir, PENDING_SUBDIR)
      try {
        for (const f of readdirSync(pend)) {
          if (f.startsWith(INFLIGHT_PREFIX) || f.endsWith('.json')) staged.push(join(pend, f))
        }
      } catch {}
    }
    staged.sort((a, b) => {
      const ka = timeSortKey(basename(a)), kb = timeSortKey(basename(b))
      return ka < kb ? -1 : ka > kb ? 1 : a < b ? -1 : a > b ? 1 : 0
    })
    for (const p of staged) this.enqueueFile(p)
    if (this.queue.length > 0) void this.ensure().then(() => this.pump())
  }

  private pump(): void {
    if (this.pumping || this.inFlight || this.phaseVal !== 'running' || this.queue.length === 0) return
    const item = this.queue.shift()!
    if (item.kind === 'raw') {
      this.dispatchContent(item, item.content, null)
      return
    }
    // file：队头连续的 file 段逐个读 meta（停用期间积压的陈旧合成件在 readInboxMeta 里删掉 → null），
    // 再交给 inbox_names 的纯函数规划：同 chat 的连发合并成一条注入（省轮次，治"只回最后一条"），
    // 读不了的丢弃，遇到别的 chat 即停。只映射到第一个换 chat 的项为止，积压再长也不全读。
    const now = Date.now()
    const metas = new Map<string, Record<string, any>>()
    const heads: Array<{ path: string; chatId: string | null }> = []
    let firstChat: string | null = null
    for (const q of [item, ...this.queue]) {
      if (q.kind !== 'file') break
      const m = readInboxMeta(q.path, now, logSpawn)
      const cid = m ? String(m.chat_id ?? '') : null
      if (m) metas.set(q.path, m)
      heads.push({ path: q.path, chatId: cid })
      if (cid != null && firstChat == null) firstChat = cid
      else if (cid != null && cid !== firstChat) break
    }
    const plan = planInboxBatch(heads)
    this.queue.splice(0, heads.length - plan.rest - 1)   // item 自己已 shift 出队
    for (const p of plan.drop) rmSync(p, { force: true })
    if (plan.merge.length === 0) return
    const merged = plan.merge.map(p => metas.get(p)!).filter(Boolean)
    const content = this.buildDeliveryContent(merged)
    // 拍照轮次只在投递时经 /photo_round 增加；要等它的返回决定轮次，故单独走异步分支。
    // 失败/关闭照常投递、不追加标记；等待期间 pumping 挡住下一次投递。
    const photo = this.photoRequestFor(merged[0])
    if (photo) {
      this.pumping = true
      void this.photoRound(photo.chatId, photo.deliveryKey)
        .then(round => {
          const mark = round != null
            ? `[auto-photo] 第${round}轮：本轮回复按 image 技能的「旁观摄影机视角」规则生图并附上 2 张（同一场景不同机位）；正文不写镜头、拍照或动作描述。`
            : ''
          this.finishFileDelivery(plan, merged, mark ? this.injectDeliveryMarks(content, [mark]) : content)
        })
        .catch(() => this.finishFileDelivery(plan, merged, content))
        .finally(() => { this.pumping = false; this.pump() })
      return
    }
    this.finishFileDelivery(plan, merged, content)
  }

  /** 投递收尾（同步路径与异步拍照路径共用）：删合并原件、写 sidecar、交给 dispatchContent。 */
  private finishFileDelivery(plan: { merge: string[]; chatId: string | null }, merged: any[], content: string): void {
    for (const p of plan.merge.slice(1)) rmSync(p, { force: true })
    // reply 默认路由：worker-plugin 的 lastChatId 改由该文件驱动
    this.writeLastChatId(plan.chatId ?? '')
    // party-camera/承诺工具：投递件 sidecar（当前聊天、消息 id、聚会改投、最近真人）
    this.writeDeliverySidecar(merged)
    this.dispatchContent({ kind: 'file', path: plan.merge[0] }, content, plan.chatId)
  }

  /** 投递件正文：buildContent + party 标记（固定文案；拍照标记由异步分支追加）。 */
  private buildDeliveryContent(metas: any[]): string {
    const base = metas.map(m => this.buildContent(m)).join('\n\n')
    if (metas.length === 0) return base
    const first = metas[0]
    const partyChat = typeof first.party_chat_id === 'string' && first.party_chat_id !== '' ? first.party_chat_id : ''
    let mark = ''
    if (first.party_ambiguous === true) {
      mark = '[party-ambiguous] 你有多个群在聚会，本轮按普通私聊回复。'
    } else if (partyChat !== '') {
      mark = `[party] 线下聚会进行中（群 ${partyChat}）：实质回复默认发到群聊；可先在私聊回一句简短引导（如“在群里说”），私聊最多一句；要私聊回应时，reply 显式带 chat_id=${String(first.chat_id ?? '')}。`
    }
    return mark ? this.injectDeliveryMarks(base, [mark]) : base
  }

  /** 系统标记插在场景标注之前（clear_summary 按场景标注切注入）；正文里用户写的同名方括号已转全角。 */
  private injectDeliveryMarks(content: string, marks: string[]): string {
    const block = `${marks.filter(Boolean).join('\n')}\n`
    const at = content.search(/【(?:私聊|群聊)】<channel\b/)
    if (at >= 0) return content.slice(0, at) + block + content.slice(at)
    const open = content.indexOf('<channel')
    if (open >= 0) return content.slice(0, open) + block + content.slice(open)
    return block + content
  }

  /** 该投递件是否要增加拍照轮次；不满足则返回 null。 */
  private photoRequestFor(meta: any): { chatId: string; deliveryKey: string } | null {
    if (!meta || !meta.photo_mode || typeof meta.photo_mode !== 'object' || meta.photo_mode.active !== true || meta.photo_mode_off === true) return null
    const deliveryKey = photoDeliveryKeyFromMeta(meta)
    if (deliveryKey === '') return null
    return { chatId: String(meta.chat_id ?? ''), deliveryKey }
  }

  /** POST /photo_round（本机 dispatcher）；非 2xx/网络失败返回 null，只记状态码。 */
  private async photoRound(chatId: string, deliveryKey: string): Promise<number | null> {
    try {
      const token = readFileSync(join(this.channelDir, '.api-token'), 'utf8').trim()
      if (!token) return null
      const r = await fetch(`${DISPATCHER_URL}/photo_round`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Api-Token': token },
        body: JSON.stringify({ chat_id: chatId, delivery_key: deliveryKey }),
        signal: AbortSignal.timeout(3000),
      })
      if (!r.ok) { process.stderr.write(`worker-manager: photo_round_failed status=${r.status}\n`); return null }
      const out = await r.json() as any
      return out && out.ok === true && out.active === true && typeof out.round === 'number' && Number.isFinite(out.round) ? out.round : null
    } catch (e) {
      process.stderr.write(`worker-manager: photo_round_failed reason=${e instanceof Error ? e.name : 'unknown'}\n`)
      return null
    }
  }

  /** 写 <CHANNEL_DIR>/.last-delivery.json；worker-plugin 只读它做 reply 路由/party/承诺工具绑定。 */
  private writeDeliverySidecar(metas: any[]): void {
    if (metas.length === 0) return
    const first = metas[0]
    const chatId = String(first.chat_id ?? '')
    if (!chatId) return
    const tsMsOf = (m: any): number => {
      const t = Date.parse(String(m?.ts ?? ''))
      return Number.isFinite(t) && t > 0 ? t : (this.now ? this.now() : Date.now())
    }
    let allowFrom: unknown = []
    try { allowFrom = (JSON.parse(readFileSync(join(this.channelDir, 'access.json'), 'utf8')) as any)?.allowFrom ?? [] } catch {}
    for (const m of metas) this.lastHuman = nextLastHuman(this.lastHuman, m, tsMsOf(m), false, allowFrom)
    const ids: string[] = []
    const promiseIds: string[] = []
    let promiseDue = false
    for (const m of metas) {
      if (Array.isArray(m.merged_message_ids)) for (const id of m.merged_message_ids) if (id != null) ids.push(String(id))
      else if (m.message_id != null) ids.push(String(m.message_id))
      if (Array.isArray(m.promise_ids)) for (const id of m.promise_ids) if (typeof id === 'string') promiseIds.push(id)
      if (typeof m.promise_id === 'string') promiseIds.push(m.promise_id)
      if (isPromiseDueInbox(m)) promiseDue = true
    }
    const doc = {
      v: 1,
      chat_id: chatId,
      message_id: [...new Set(ids)].join(','),
      party_chat_id: typeof first.party_chat_id === 'string' ? first.party_chat_id : '',
      party_ambiguous: first.party_ambiguous === true,
      promise_ids: [...new Set(promiseIds)],
      promise_due: promiseDue,
      human_chat_id: this.lastHuman?.chatId ?? '',
      human_user_id: this.lastHuman?.userId ?? '',
      at_ms: this.now ? this.now() : Date.now(),
    }
    try {
      const p = join(this.channelDir, '.last-delivery.json')
      writeFileSync(p + '.tmp', JSON.stringify(doc))
      renameSync(p + '.tmp', p)
    } catch {}
  }

  private dispatchContent(item: QueueItem, content: string, chatId: string | null): void {
    if (!this.proc || typeof this.proc.stdin?.write !== 'function') { this.queue.unshift(item); return }
    // H2b：投递前把原件改名成 <dir>/.pending/.inflight-<attempt>-<stem>.part（不删原件；非 .json 后缀）。
    // 已在飞件（重放用的就是这个路径）attempt 从名字里读回，保证序号不回退。
    if (item.kind === 'file') {
      const parsed = parseInflightName(basename(item.path))
      const attempt = parsed ? parsed.attempt : 0
      const dst = inflightPathFor(item.path, attempt)
      try {
        mkdirSync(dirname(dst), { recursive: true })
        if (dst !== item.path) renameSync(item.path, dst)
        if (existsSync(dst)) item.path = dst
      } catch {}
      this.inFlightAttempt = attempt
    } else {
      this.inFlightAttempt = 0
    }
    this.inFlightChatId = chatId || ''
    this.inFlight = item
    this.inFlightContent = content
    this.inFlightSince = Date.now()
    this.lastEventAt = (this.now ?? Date.now)()
    this.resultTimer = this.timer.setTimeout(() => this.checkStuck(), STALL_MS + 1000)
    // 冷轮前置 reply 提醒（slash 命令本身不加，且提醒只加一次就清标记）
    const isSlash = item.kind === 'raw' && item.label.startsWith('slash')
    let payload = content
    if (this.needsReplyReminder && !isSlash) {
      this.needsReplyReminder = false
      payload = `${REPLY_REMINDER}\n\n${content}`
    }
    const line = JSON.stringify({ type: 'user', message: { role: 'user', content: payload } })
    this.proc.stdin.write(line + '\n')
    logChat(`👤 ${content.slice(0, 500)}${content.length > 500 ? '…' : ''}`)
  }

  private writeLastChatId(chatId: string): void {
    try {
      const p = join(CHANNEL_DIR, '.last-chat-id')
      writeFileSync(p + '.tmp', chatId)
      renameSync(p + '.tmp', p)
    } catch {}
  }

  // inbox meta → 组装后的 content（worker-plugin.ts deliverFile 的组装逻辑逐字段迁入）
  private buildContent(meta: any): string {
    const text: string = typeof meta.text === 'string' ? meta.text : ''
    const isBotSender = meta.is_bot_sender === true
    const userTag = isBotSender
      ? `peer-bot-${String(meta.from_id ?? '')}`
      : (meta.sender_username ?? meta.from_username ?? String(meta.from_id ?? ''))
    const chatIdStr = String(meta.chat_id)
    const scene = (meta.scene === 'group' || meta.scene === 'private')
      ? meta.scene
      : (chatIdStr.startsWith('-') ? 'group' : 'private')
    const notifMeta: Record<string, unknown> = {
      chat_id: chatIdStr, scene, user: userTag,
      user_id: String(meta.from_id ?? ''), chat_type: meta.chat_type ?? 'private', ts: meta.ts,
    }
    if (meta.message_id != null) notifMeta.message_id = String(meta.message_id)
    if (meta.sender_username) notifMeta.sender_username = meta.sender_username
    if (meta.image_path) notifMeta.image_path = meta.image_path
    if (meta.voice_text) notifMeta.voice_text = meta.voice_text
    if (meta.attachment_kind) notifMeta.attachment_kind = meta.attachment_kind
    if (meta.attachment_file_id) notifMeta.attachment_file_id = meta.attachment_file_id
    if (meta.attachment_size != null) notifMeta.attachment_size = String(meta.attachment_size)
    if (meta.attachment_mime) notifMeta.attachment_mime = meta.attachment_mime
    if (meta.attachment_name) notifMeta.attachment_name = meta.attachment_name
    if (meta.reply_to != null) notifMeta.reply_to = String(meta.reply_to)

    const sceneTag = scene === 'group' ? '【群聊】' : '【私聊】'
    // party-camera：用户正文里的系统标记转全角，模型只信系统追加的标记
    const safeText = escapeUserMarkers(text)
    const body = isBotSender && meta.sender_username
      ? `[from peer bot @${meta.sender_username}]\n${safeText}`
      : safeText
    // 时间标注：消息只带 UTC 机器时间戳，模型会顺着上下文惯性以为还是早上聊天那会儿。
    // 时间取 meta.ts（消息自身时刻，不是投递时刻）；缺失/非法 → 用当前时刻。
    const parsedTs = Date.parse(String(meta.ts ?? ''))
    const tsMs = Number.isFinite(parsedTs) && parsedTs > 0 ? parsedTs : Date.now()
    // desync ③b：间隔 = 距本 bot 此前收到的最近一条真人消息（群/私聊共用）；合成消息不重置
    const prevHuman = inboundState._human
    inboundState = applyInboundMarks(inboundState, chatIdStr, meta, tsMs)
    const timePrefix = buildTimePrefix(tsMs, prevHuman)
    rememberInboundTs(chatIdStr, tsMs)
    // 真人私聊消息前注入关系数值提示（群聊/peer/导演 inject 不注入——与旧行为一致）
    const isHumanPrivate = !isBotSender && scene === 'private'
    let relPrefix = ''
    if (isHumanPrivate) {
      try {
        const d = JSON.parse(readFileSync(join(CHANNEL_DIR, 'relationship.json'), 'utf8'))
        if (typeof d.prompt_snippet === 'string' && d.prompt_snippet) relPrefix = `${d.prompt_snippet}\n\n`
      } catch {}
    }
    // 情境锚点：她此刻的学期状态 + 正在做的事。这些原先只进主动消息的提示词，被动聊天
    // 零锚点 → 用户一句"骗鬼呢"就能把她的世界观带崩。桥挂时返回空串，这条不带锚点。
    const situLine = isHumanPrivate ? situAnchorLine(BOT) : ''
    // 被晾档案（她上课/工作时被晾过）：下课后的第一条消息带上一段迟到反应，带完即清。
    // 位置刻意在 <channel> 开标签之前——那之前的一切都算注入，clear_summary 会连同
    // 关系块/时刻行一起剥掉，真人这条消息不会因为夹带 [hang-check] 而被整条丢出摘要。
    // ponytail: 取不到一律空串，绝不阻断投递；worker 恰好死在这一刻时最多少带一次口吻。
    const late = takeHangArchive(CHANNEL_DIR, chatIdStr)
    // meta 以 <channel> 标签形态附在正文后（替代 channel notification 的 params.meta，
    // 让 model 依旧能读到 chat_id/message_id/附件等字段）
    const metaAttrs = Object.entries(notifMeta)
      .filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => `${k}="${String(v).replace(/"/g, '&quot;')}"`)
      .join(' ')
    // 场景标注必须紧贴 <channel>，注入的几行一律排在它前面（摘要按开标签一刀切）
    // H-27：正文里的 <channel / </channel 前缀先转义，别让它破坏包装结构（其余字符逐字保留）
    const safeBody = escapeChannelWrapper(body)
    const content =
      `${relPrefix}${timePrefix}${situLine}${late ? `${late}\n` : ''}${sceneTag}<channel ${metaAttrs}>\n${safeBody}\n</channel>`
    return content
  }
}

// 单例（dispatcher.ts import 使用）
let _manager: WorkerManager | null = null
export function getManager(): WorkerManager {
  if (!_manager) {
    // 测试模式 fail-closed（INTERFACE §11.2）：开关未设时直接通过，生产零影响。
    // 把 manager 实际会用的 CHANNEL_DIR / 派给 worker 的 dispatcher 地址一并交检，防测试落到生产端口。
    const tm = testModeCheck({ ...process.env, CHANNEL_DIR, TELEGRAM_DISPATCHER_URL: DISPATCHER_URL })
    if (!tm.ok) { process.stderr.write(`test_mode: refuse ${tm.reason}\n`); process.exit(97) }
    _manager = new WorkerManager()
  }
  return _manager
}
