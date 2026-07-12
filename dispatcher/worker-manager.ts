/**
 * worker-manager.ts — 跨平台 worker 生命周期管理（替代 tmux + spawn-worker.sh）。
 *
 * worker = 常驻 headless claude 子进程：
 *   claude -p --input-format stream-json --output-format stream-json
 * 入站：inbox 文件（dispatcher/director/moments/self-initiate 照旧写）→ 本模块
 * fs.watch → 组装（关系提示+场景标签+meta）→ 写 claude stdin。
 * 出站：worker 里的 MCP reply 工具 → dispatcher /send（不变）。
 * slash：/compact 等当 user 消息写 stdin（无需 tmux send-keys）。
 * 就绪：stdout 的 system/init 事件（无需 capture-pane 抓屏）。
 * 观测：logs/chat.log（人话对话流，tail -f 替代 tmux attach）+ logs/stream.jsonl（原始事件）。
 *
 * 与 dispatcher.ts 同进程（1 dispatcher : 1 worker）。
 */

import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from 'child_process'
import { createHash } from 'crypto'
import {
  readFileSync, writeFileSync, appendFileSync, readdirSync, rmSync, renameSync,
  mkdirSync, existsSync, statSync, copyFileSync, watch, type FSWatcher,
} from 'fs'
import { join, delimiter } from 'path'
import { homedir, tmpdir, platform } from 'os'

// ─── 配置（与 dispatcher.ts 同源的 env）────────────────────────────────
const BOT = process.env.BOT_NAME || ''
const CHANNEL_DIR = process.env.CHANNEL_DIR || join(homedir(), '.claude', 'channels', BOT)
const DISPATCHER_URL = `http://127.0.0.1:${process.env.DISPATCHER_PORT || '17801'}`

// ─── UUIDv5（纯 TS，替代两处 python3 shellout）─────────────────────────
// **必须与 chat_history.py 的 _BOT_NAMESPACES 完全一致**，否则丢记忆。
export const BOT_NAMESPACES: Record<string, string> = {
  chenlulu: '550e8400-e29b-41d4-a716-446655440001',
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
// win:  C:\Users\you\.claude\channels\bot → C--Users-you--claude-channels-bot
// ⚠️ Windows 真实规则以 Phase 0 实测为准；spawn 后有断言兜底（不匹配 fail-loud）。
export function projectSlug(absDir: string): string {
  return absDir.replace(/[^a-zA-Z0-9]/g, '-')
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

// 判断 pid 是否"像我们的 worker 进程"——孤儿清理前校验，防 Windows PID 复用后误杀无辜进程树。
function looksLikeWorker(pid: number): boolean {
  if (!pid || pid <= 0) return false
  try {
    if (platform() === 'win32') {
      const r = spawnSync('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], { encoding: 'utf8' })
      const name = (r.stdout || '').split(',')[0].replace(/"/g, '').toLowerCase()
      return ['cmd.exe', 'claude.exe', 'node.exe', 'bun.exe'].includes(name)
    }
    const r = spawnSync('ps', ['-o', 'comm=', '-p', String(pid)], { encoding: 'utf8' })
    return /claude|node|bun|cmd/i.test(r.stdout || '')
  } catch { return false }
}

// ─── claude 可执行解析 ─────────────────────────────────────────────────
function resolveClaude(): { bin: string; viaCmd: boolean } {
  const override = process.env.CLAUDE_BIN
  if (override) return { bin: override, viaCmd: /\.(cmd|bat)$/i.test(override) }
  const isWin = platform() === 'win32'
  const probe = spawnSync(isWin ? 'where' : 'which', ['claude'], { encoding: 'utf8' })
  const lines = (probe.stdout || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean)
  // Windows: npm 装的 claude 会同时列出无扩展名的 bash shim(在前) + claude.cmd。
  // 直接 spawn 那个 bash shim → WinError 193。必须优先挑可执行的 .exe/.cmd/.bat。
  const found = isWin
    ? (lines.find(l => /\.(exe|cmd|bat)$/i.test(l)) || lines[0])
    : lines[0]
  if (found) return { bin: found, viaCmd: /\.(cmd|bat)$/i.test(found) }
  return { bin: 'claude', viaCmd: false } // 交给 PATH，起不来会在 spawn error 里报清楚
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
    const d = JSON.parse(readFileSync(join(homedir(), '.claude', 'settings.json'), 'utf8'))
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

function cliVersion(claudeBin: string, viaCmd: boolean): string {
  const r = viaCmd
    ? spawnSync('cmd', ['/c', claudeBin, '--version'], { encoding: 'utf8' })
    : spawnSync(claudeBin, ['--version'], { encoding: 'utf8' })
  return (r.stdout || '').split('\n')[0].trim()
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

// thinking 剥离（防 resume 400 Invalid signature）：>12h 老 thinking 剥掉，stripAll 时全清。保 .bak
export function stripThinking(jsonlPath: string, stripAll: boolean): void {
  let txt: string
  try { txt = readFileSync(jsonlPath, 'utf8') } catch { return }
  if (!txt.includes('"type":"thinking"')) return
  const cutoff = Date.now() / 1000 - 12 * 3600
  try { copyFileSync(jsonlPath, `${jsonlPath}.bak.${Math.floor(Date.now() / 1000)}`) } catch {}
  const kept: string[] = []
  let nStripped = 0, nKept = 0
  for (const line of txt.split('\n')) {
    if (!line.trim()) { kept.push(line); continue }
    let o: any
    try { o = JSON.parse(line) } catch { kept.push(line); continue }
    let ts = 0
    try { ts = new Date(String(o.timestamp || '').replace('Z', '+00:00')).getTime() / 1000 || 0 } catch {}
    const msg = o.message || {}
    const c = msg.content
    if (Array.isArray(c)) {
      const nc = c.filter((x: any) => {
        if (x && typeof x === 'object' && x.type === 'thinking') {
          if (stripAll || (ts && ts < cutoff)) { nStripped++; return false }
          nKept++
        }
        return true
      })
      if (nc.length === 0) continue // 整条只有老 thinking → 删
      msg.content = nc
    }
    kept.push(JSON.stringify(o))
  }
  writeFileSync(jsonlPath, kept.join('\n') + '\n')
  logSpawn(`stripped ${nStripped} old thinking, kept ${nKept} recent`)
}

// 尺寸守卫：真实 context >170k tokens → 结构化压缩到 ~100k（丢最老 user/assistant，
// 其余行保序保留；纯文件操作不调 LLM）。老对话在 .bak。
export function compactSessionIfHuge(jsonlPath: string): void {
  let lines: string[]
  try { lines = readFileSync(jsonlPath, 'utf8').split('\n') } catch { return }
  const sizeOf = (line: string): number => {
    try {
      const o = JSON.parse(line)
      if (o.type !== 'user' && o.type !== 'assistant') return 0
      const c = (o.message || {}).content
      return c == null ? 0 : Math.floor(JSON.stringify(c).length / 3)
    } catch { return 0 }
  }
  const total = lines.reduce((s, l) => s + (l.trim() ? sizeOf(l) : 0), 0)
  if (total <= 170_000) return
  logSpawn(`session ctx ~${total} tokens > 170k，结构化压缩…`)
  try { copyFileSync(jsonlPath, `${jsonlPath}.bak.${Math.floor(Date.now() / 1000)}`) } catch {}
  // 从最新往回给 user/assistant 行配额（~100k），配额外的老对话丢弃；非对话行全保留。
  let budget = 100_000
  const keepDialog = new Set<number>()
  for (let i = lines.length - 1; i >= 0; i--) {
    const sz = lines[i].trim() ? sizeOf(lines[i]) : 0
    if (sz === 0) continue
    if (budget - sz < 0) break
    budget -= sz
    keepDialog.add(i)
  }
  const kept = lines.filter((l, i) => !l.trim() || sizeOf(l) === 0 || keepDialog.has(i))
  writeFileSync(jsonlPath, kept.join('\n'))
}

// worker 子进程 env：清旧 ccp 残留 → 叠 settings.json env → 代理重定向 → 身份变量
function buildEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env }
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
  return env
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

// ─── 队列条目 ──────────────────────────────────────────────────────────
type QueueItem =
  | { kind: 'file'; path: string }
  | { kind: 'raw'; content: string; label: string }   // slash / /inject 调试注入

// ─── WorkerManager ────────────────────────────────────────────────────
const RESULT_TIMEOUT_MS = 5 * 60_000        // result 事件超时 → 认定卡死
const BACKOFF_MS = [1_000, 5_000, 30_000, 60_000]

// 冷轮（全新会话 / /clear 之后）无历史示范惯性，模型易直接吐文本忘调 reply → 用户收不到。
// 只在冷轮给下一条真人消息前置一次性提醒，不动沉默权（选择不回仍可不调 reply）。
const REPLY_REMINDER =
  '【系统提醒·仅本轮】你在 Telegram 上和用户对话。要让对方真正收到你的话，'
  + '必须调用 reply 工具发送——直接输出文本用户是看不到的。'
  + '（若你按当下人设选择不回复，那就不调 reply、直接结束本轮，这没问题。）'

export class WorkerManager {
  private proc: ChildProcessWithoutNullStreams | null = null
  private phase: 'stopped' | 'starting' | 'ready' = 'stopped'
  private inFlight: QueueItem | null = null
  private inFlightSince = 0
  private queue: QueueItem[] = []
  private restartAttempt = 0
  private intentionalKill = false
  private needsReplyReminder = false      // 冷轮标记：下一条真人消息前置 REPLY_REMINDER
  private stdoutBuf = ''
  private watchers: FSWatcher[] = []
  private resultTimer: ReturnType<typeof setTimeout> | null = null
  readonly sessionUuid: string

  constructor() {
    this.sessionUuid = unifiedSessionUuid(BOT)
    mkdirSync(join(CHANNEL_DIR, 'inbox'), { recursive: true })
    this.watchInboxes()
    setInterval(() => this.drainInbox(), 5_000)  // Windows fs.watch 语义差异保险
    setInterval(() => this.checkStuck(), 30_000)
  }

  // ── 对外接口 ─────────────────────────────────────────────────────
  isAlive(): boolean { return this.proc !== null && this.phase !== 'stopped' }

  async ensure(): Promise<void> {
    if (this.isAlive()) return
    await this.spawnWorker()
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
    if (this.proc?.pid) killTree(this.proc.pid, !this.intentionalKill)
    try { rmSync(join(CHANNEL_DIR, '.worker.pid'), { force: true }) } catch {}  // 进程已死，别留陈旧 pid
    this.phase = 'stopped'
    this.proc = null
  }

  status(): Record<string, unknown> {
    return {
      bot: BOT, phase: this.phase, session_uuid: this.sessionUuid,
      pid: this.proc?.pid ?? null, queue_depth: this.queue.length,
      in_flight: this.inFlight ? (this.inFlight.kind === 'file' ? this.inFlight.path : this.inFlight.label) : null,
    }
  }

  // ── spawn（spawn-worker.sh 等价物）───────────────────────────────
  private async spawnWorker(): Promise<void> {
    if (this.phase === 'starting') return
    this.phase = 'starting'
    this.intentionalKill = false

    // 孤儿防护：上次 dispatcher 崩溃可能留下没被带走的 claude（双进程写同一 jsonl = 数据损坏）
    const pidFile = join(CHANNEL_DIR, '.worker.pid')
    try {
      const stale = parseInt(readFileSync(pidFile, 'utf8').trim(), 10)
      // ⚠️ 杀前必须校验：陈旧 pid 可能已被系统复用给无辜进程（Windows PID 复用频繁），
      // 直接 taskkill /T /F 会误杀整树。只有映像名像 worker 才杀。
      if (stale > 0 && looksLikeWorker(stale)) { killTree(stale); logSpawn(`杀掉孤儿 worker 进程树 pid=${stale}`) }
      rmSync(pidFile, { force: true })  // 无论杀没杀，陈旧 pid 文件都清掉
    } catch {}

    const botDir = CHANNEL_DIR
    const slug = projectSlug(botDir)
    const projDir = join(homedir(), '.claude', 'projects', slug)
    const jsonl = join(projDir, `${this.sessionUuid}.jsonl`)

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

    logSpawn(`spawn worker: ${resume ? '--resume' : '--session-id'} ${this.sessionUuid} (claude=${claude.bin})`)
    // Windows .cmd：走 cmd /s /c + windowsVerbatimArguments，自己给含空格/特殊字符的参数加引号
    // （路径可能含空格如 C:\Users\My Name\...，node 默认加引号规则在 cmd /c 下会碎）。
    let proc
    if (claude.viaCmd) {
      const line = [claude.bin, ...args].map(a => /[\s&|<>^()"]/.test(a) ? `"${a}"` : a).join(' ')
      proc = spawn('cmd', ['/s', '/c', line], { cwd: botDir, env, stdio: ['pipe', 'pipe', 'pipe'], windowsVerbatimArguments: true })
    } else {
      proc = spawn(claude.bin, args, { cwd: botDir, env, stdio: ['pipe', 'pipe', 'pipe'] })
    }
    this.proc = proc as ChildProcessWithoutNullStreams
    try { writeFileSync(pidFile, String(proc.pid ?? '')) } catch {}

    proc.stdout.on('data', (chunk: Buffer) => this.onStdout(chunk))
    proc.stderr.on('data', (chunk: Buffer) => {
      const s = chunk.toString().trim()
      if (s) logStream({ type: '_stderr', text: s.slice(0, 2000) })
    })
    proc.on('error', (err) => { logSpawn(`spawn error: ${err}`); this.onExit(-1) })
    proc.on('exit', (code) => this.onExit(code ?? 0))

    // spawn 即 ready：stdin 管道会缓冲，claude 启动完自然消费第一条消息。
    // （不能等 system/init——headless 下它在收到第一条输入后才发，等它=死锁）
    this.phase = 'ready'
    logSpawn('worker spawned, 管道就绪')
    this.pump()

    // slug 断言：spawn 后 claude 应在预期 projects 目录写 session；不匹配 = slugify 规则错 = 丢记忆
    setTimeout(() => {
      if (this.phase === 'ready' && !existsSync(projDir)) {
        logSpawn(`🔴 slug 断言失败：预期 projects 目录不存在 ${projDir} — slugify 规则与 CLI 不一致，可能丢记忆！`)
      }
    }, 30_000)
  }

  // ── stdout 事件流 ─────────────────────────────────────────────────
  private onStdout(chunk: Buffer): void {
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
      this.inFlight = null
      if (this.resultTimer) { clearTimeout(this.resultTimer); this.resultTimer = null }
      this.pump()
      return
    }
  }

  private onExit(code: number): void {
    logSpawn(`worker exit code=${code}${this.intentionalKill ? ' (intentional)' : ''}`)
    try { rmSync(join(CHANNEL_DIR, '.worker.pid'), { force: true }) } catch {}  // 进程死了，pid 文件作废
    this.proc = null
    this.phase = 'stopped'
    const wasInFlight = this.inFlight
    this.inFlight = null
    if (this.resultTimer) { clearTimeout(this.resultTimer); this.resultTimer = null }
    if (this.intentionalKill) { this.intentionalKill = false; return }
    // 在飞那条没跑完 → 重投队头（inbox 文件已删，raw 条目内容还在内存里）
    if (wasInFlight && wasInFlight.kind === 'raw') this.queue.unshift(wasInFlight)
    const delay = BACKOFF_MS[Math.min(this.restartAttempt, BACKOFF_MS.length - 1)]
    this.restartAttempt++
    logSpawn(`${delay}ms 后自动 --resume 重启 (attempt ${this.restartAttempt})`)
    setTimeout(() => { void this.spawnWorker().then(() => this.drainInbox()) }, delay)
  }

  private checkStuck(): void {
    if (this.inFlight && Date.now() - this.inFlightSince > RESULT_TIMEOUT_MS) {
      logSpawn(`result 超时 ${RESULT_TIMEOUT_MS}ms — kill + resume 重启`)
      this.kill()  // onExit 会自动重启并重投
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
          setTimeout(() => this.drainInbox(), 20) // rename 完成缓冲
        }))
      } catch {}
    }
  }

  private drainInbox(): void {
    for (const dir of this.inboxDirs()) {
      try {
        for (const f of readdirSync(dir).sort()) {
          if (!f.endsWith('.json')) continue
          const p = join(dir, f)
          if (!this.queue.some(q => q.kind === 'file' && q.path === p)
              && !(this.inFlight?.kind === 'file' && this.inFlight.path === p)) {
            this.queue.push({ kind: 'file', path: p })
          }
        }
      } catch {}
    }
    if (this.queue.length > 0) void this.ensure().then(() => this.pump())
  }

  private pump(): void {
    if (this.inFlight || this.phase !== 'ready' || this.queue.length === 0) return
    const item = this.queue.shift()!
    if (item.kind === 'raw') {
      this.dispatchContent(item, item.content, null)
      return
    }
    // file：读取+组装；同 chat 的连发消息合并成一条注入（省轮次，治"只回最后一条"）
    const first = this.readInboxMeta(item.path)
    if (!first) { rmSync(item.path, { force: true }); this.pump(); return }
    const parts = [first]
    const chatId = String(first.meta.chat_id ?? '')
    while (this.queue.length > 0 && this.queue[0].kind === 'file') {
      const nxt = this.queue[0] as { kind: 'file'; path: string }
      const m = this.readInboxMeta(nxt.path)
      if (!m) { this.queue.shift(); rmSync(nxt.path, { force: true }); continue }
      if (String(m.meta.chat_id ?? '') !== chatId) break
      parts.push(m); this.queue.shift(); rmSync(nxt.path, { force: true })
    }
    const content = parts.map(p => p.content).join('\n\n')
    // reply 默认路由：worker-plugin 的 lastChatId 改由该文件驱动
    this.writeLastChatId(chatId)
    this.dispatchContent(item, content, item.path)
  }

  private dispatchContent(item: QueueItem, content: string, deletePath: string | null): void {
    if (!this.proc || !this.proc.stdin.writable) { this.queue.unshift(item); return }
    this.inFlight = item
    this.inFlightSince = Date.now()
    this.resultTimer = setTimeout(() => this.checkStuck(), RESULT_TIMEOUT_MS + 1000)
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
    if (deletePath) rmSync(deletePath, { force: true })
  }

  private writeLastChatId(chatId: string): void {
    try {
      const p = join(CHANNEL_DIR, '.last-chat-id')
      writeFileSync(p + '.tmp', chatId)
      renameSync(p + '.tmp', p)
    } catch {}
  }

  // inbox 文件 → 组装后的 content（worker-plugin.ts deliverFile 的组装逻辑逐字段迁入）
  private readInboxMeta(path: string): { content: string; meta: Record<string, unknown> } | null {
    let meta: any
    try { meta = JSON.parse(readFileSync(path, 'utf8')) } catch { return null }
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
    const body = isBotSender && meta.sender_username
      ? `[from peer bot @${meta.sender_username}]\n${text}`
      : text
    // 真人私聊消息前注入关系数值提示（群聊/peer/导演 inject 不注入——与旧行为一致）
    let relPrefix = ''
    if (!isBotSender && scene === 'private') {
      try {
        const d = JSON.parse(readFileSync(join(CHANNEL_DIR, 'relationship.json'), 'utf8'))
        if (typeof d.prompt_snippet === 'string' && d.prompt_snippet) relPrefix = `${d.prompt_snippet}\n\n`
      } catch {}
    }
    // meta 以 <channel> 标签形态附在正文后（替代 channel notification 的 params.meta，
    // 让 model 依旧能读到 chat_id/message_id/附件等字段）
    const metaAttrs = Object.entries(notifMeta)
      .filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => `${k}="${String(v).replace(/"/g, '&quot;')}"`)
      .join(' ')
    const content = `${relPrefix}${sceneTag}<channel ${metaAttrs}>\n${body}\n</channel>`
    return { content, meta: notifMeta }
  }
}

// 单例（dispatcher.ts import 使用）
let _manager: WorkerManager | null = null
export function getManager(): WorkerManager {
  if (!_manager) _manager = new WorkerManager()
  return _manager
}
