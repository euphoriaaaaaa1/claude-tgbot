// pub-outage 黑盒验收共享夹具：假 Telegram（本机 127.0.0.1）+ 子进程真 dispatcher。
// 只依据 pub-outage-接口.md 6.5 的约定。零副作用：
//   - HOME / CHANNEL_DIR / LINK_STATE_DIR / DISABLED_DIR 全部指向本用例临时目录
//   - 子进程环境是白名单拼出来的（不继承代理变量、不继承任何真实路径）
//   - 假令牌固定 123456:TEST-FAKE-TOKEN；Telegram 基址指向本机假服务
//   - claude/tmux/launchctl/pkill/schtasks 在 PATH 里被桩替换（exit 97），绝不起真 worker
import http from 'node:http'
import net from 'node:net'
import type { Socket } from 'node:net'
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// 测试进程自己的 fetch 也不许走任何代理
for (const k of Object.keys(process.env)) if (/_proxy$/i.test(k)) delete process.env[k]
process.env.NO_PROXY = '*'
process.env.no_proxy = '*'

export const TOKEN = '123456:TEST-FAKE-TOKEN'
export const PRIVATE_CHAT = '123456789'
export const GROUP_CHAT = '-100999000111'
export const API_TOKEN = 'pubout' + '0'.repeat(58)
const HERE = dirname(fileURLToPath(import.meta.url))
export const REPO = resolve(HERE, '..', '..', '..')

export const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

export async function waitFor<T>(fn: () => T | Promise<T>, timeoutMs: number, label: string, stepMs = 200): Promise<NonNullable<T>> {
  const end = Date.now() + timeoutMs
  while (true) {
    let v: T | undefined
    try { v = await fn() } catch { v = undefined }
    if (v) return v as NonNullable<T>
    if (Date.now() > end) throw new Error(`等待超时(${timeoutMs}ms): ${label}`)
    await sleep(stepMs)
  }
}

async function freePort(): Promise<number> {
  return await new Promise<number>((res, rej) => {
    const s = net.createServer()
    s.once('error', rej)
    s.listen(0, '127.0.0.1', () => { const p = (s.address() as net.AddressInfo).port; s.close(() => res(p)) })
  })
}

// ─── 假 Telegram ────────────────────────────────────────────────────────────
// normal：按 Bot API 回包；cut：读完请求行记下方法名后立即断开；hang：接受连接永不回包；off：端口不监听
export type FakeMode = 'normal' | 'cut' | 'hang' | 'off'
export interface FakeCall { method: string; body: any; at: number }

export class FakeTelegram {
  mode: FakeMode = 'normal'
  methodMode: Record<string, FakeMode> = {}
  calls: FakeCall[] = []
  updates: any[] = []
  /** 对 send* 方法按序号返回自定义回应（返回 null 表示走正常回包） */
  sendResponder: ((method: string, n: number) => { status: number; body: any } | null) | null = null
  /** 每个方法被收到时同步回调（用于在"回执发出的那一刻"读外部状态） */
  onMethod: ((method: string, body: any) => void) | null = null
  port = 0
  private server: http.Server | null = null
  private sockets = new Set<Socket>()
  private mid = 100
  private sendCount = 0
  private updateId = 1000

  get url(): string { return `http://127.0.0.1:${this.port}` }
  get methods(): string[] { return this.calls.map(c => c.method) }
  clearCalls(): void { this.calls.length = 0 }
  callsOf(method: string): FakeCall[] { return this.calls.filter(c => c.method === method) }
  sendLikeCalls(): string[] { return this.methods.filter(m => m.startsWith('send')) }

  async listen(): Promise<void> {
    if (this.server) return
    if (!this.port) this.port = await freePort()
    const srv = http.createServer((req, res) => this.handle(req, res))
    srv.on('connection', (s: Socket) => { this.sockets.add(s); s.on('close', () => this.sockets.delete(s)) })
    await new Promise<void>((ok, bad) => { srv.once('error', bad); srv.listen(this.port, '127.0.0.1', () => ok()) })
    this.server = srv
  }

  async close(): Promise<void> {
    const srv = this.server
    this.server = null
    for (const s of this.sockets) { try { s.destroy() } catch { /* ignore */ } }
    this.sockets.clear()
    if (srv) await new Promise<void>(r => srv.close(() => r()))
  }

  async setMode(m: FakeMode): Promise<void> {
    this.mode = m
    if (m === 'off') await this.close()
    else await this.listen()
  }

  /** 推一条真人（或 bot）消息给 getUpdates */
  pushText(opts: { chatId: string; fromId: string; text: string; chatType?: string; isBot?: boolean; username?: string }): void {
    const id = ++this.updateId
    this.updates.push({
      update_id: id,
      message: {
        message_id: id,
        from: { id: Number(opts.fromId), is_bot: !!opts.isBot, first_name: 'U', username: opts.username ?? 'someone' },
        chat: { id: Number(opts.chatId), type: opts.chatType ?? 'private' },
        date: Math.floor(Date.now() / 1000),
        text: opts.text,
      },
    })
  }

  private handle(req: http.IncomingMessage, res: http.ServerResponse): void {
    const m = /^\/(?:file\/)?bot[^/]*\/([A-Za-z]+)/.exec(req.url ?? '')
    const method = m ? m[1] : ''
    const mode = this.methodMode[method] ?? this.mode
    if (mode === 'cut') {
      this.calls.push({ method, body: null, at: Date.now() })
      try { this.onMethod?.(method, null) } catch { /* ignore */ }
      req.socket.destroy()
      return
    }
    if (mode === 'hang') {
      this.calls.push({ method, body: null, at: Date.now() })
      try { this.onMethod?.(method, null) } catch { /* ignore */ }
      req.resume()
      return
    }
    const chunks: Buffer[] = []
    req.on('data', c => chunks.push(Buffer.from(c)))
    req.on('end', () => {
      let body: any = null
      try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch { /* multipart 等 */ }
      this.calls.push({ method, body, at: Date.now() })
      try { this.onMethod?.(method, body) } catch { /* ignore */ }
      void this.respond(method, body, req, res)
    })
  }

  private reply(res: http.ServerResponse, status: number, obj: unknown): void {
    try { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)) } catch { /* ignore */ }
  }

  private async respond(method: string, body: any, req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    if (method === 'getMe') return this.reply(res, 200, { ok: true, result: { id: 123456, is_bot: true, first_name: 'Fake', username: 'fake_outage_bot' } })
    if (method === 'getUpdates') {
      let gone = false
      res.on('close', () => { gone = true })
      const end = Date.now() + 1000
      while (!this.updates.length && !gone && Date.now() < end) await sleep(40)
      if (gone) return
      const out = this.updates.splice(0, 100)
      return this.reply(res, 200, { ok: true, result: out })
    }
    if (method.startsWith('send') && method !== 'sendChatAction') {
      const n = ++this.sendCount
      const custom = this.sendResponder?.(method, n) ?? null
      if (custom) return this.reply(res, custom.status, custom.body)
      return this.reply(res, 200, {
        ok: true,
        result: { message_id: ++this.mid, date: Math.floor(Date.now() / 1000), chat: { id: Number(body?.chat_id ?? 1), type: 'private' }, text: body?.text },
      })
    }
    if (method === 'getFile') return this.reply(res, 200, { ok: true, result: { file_id: 'F', file_path: 'x/y.bin', file_size: 1 } })
    return this.reply(res, 200, { ok: true, result: true })
  }
}

// ─── 真 dispatcher 子进程 ─────────────────────────────────────────────────────
const live = new Set<Harness>()
process.on('exit', () => { for (const h of live) h.killNow() })

export interface HarnessOpts {
  bot?: string
  access?: Record<string, unknown>
  fakeMode?: FakeMode
  /** 启动前预置文件 */
  preStart?: (h: Harness) => void
  extraEnv?: Record<string, string>
}

export class Harness {
  base = ''
  home = ''
  channel = ''
  linkState = ''
  disabled = ''
  bot = 'outagebot'
  port = 0
  fake = new FakeTelegram()
  proc: ReturnType<typeof Bun.spawn> | null = null
  errBuf = ''
  outBuf = ''
  private opts: HarnessOpts = {}

  static async create(opts: HarnessOpts = {}): Promise<Harness> {
    const h = new Harness()
    h.opts = opts
    h.bot = opts.bot ?? 'outagebot'
    h.base = mkdtempSync(join(tmpdir(), 'pubout-'))
    h.home = join(h.base, 'home')
    h.channel = join(h.base, 'channel')
    h.linkState = join(h.base, 'linkstate')
    h.disabled = join(h.base, 'disabled')
    for (const d of [h.home, h.channel, join(h.channel, 'media'), h.disabled, join(h.base, 'configs'), join(h.base, 'stub')]) mkdirSync(d, { recursive: true })
    for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'schtasks']) {
      const p = join(h.base, 'stub', n)
      writeFileSync(p, `#!/bin/sh\necho "test_mode: real ${n} called" >&2\nexit 97\n`)
      chmodSync(p, 0o755)
    }
    writeFileSync(join(h.channel, 'access.json'), JSON.stringify({
      allowFrom: [PRIVATE_CHAT], groups: { [GROUP_CHAT]: { title: '测试群' } }, ...(opts.access ?? {}),
    }))
    writeFileSync(join(h.channel, '.api-token'), API_TOKEN, { mode: 0o600 })
    h.port = await freePort()
    await h.fake.setMode(opts.fakeMode ?? 'normal')
    if ((opts.fakeMode ?? 'normal') === 'off') { await h.fake.listen(); await h.fake.close() } // 占一个端口号，随后不监听
    live.add(h)
    opts.preStart?.(h)
    return h
  }

  /** 一份真媒体文件（落在 CHANNEL_DIR/media 内） */
  makeMedia(name: string, kind: 'png' | 'pdf'): string {
    const p = join(this.channel, 'media', name)
    if (kind === 'png') {
      writeFileSync(p, Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f0005000201a5f645400000000049454e44ae426082', 'hex'))
    } else {
      writeFileSync(p, '%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n')
    }
    return p
  }

  start(): void {
    const env: Record<string, string> = {
      PATH: `${join(this.base, 'stub')}:${dirname(process.execPath)}:/usr/bin:/bin:/usr/sbin:/sbin`,
      HOME: this.home,
      USERPROFILE: this.home,
      TMPDIR: process.env.TMPDIR ?? '/tmp',
      LANG: 'en_US.UTF-8',
      NO_PROXY: '*',
      no_proxy: '*',
      CHANNEL_DIR: this.channel,
      LINK_STATE_DIR: this.linkState,
      DISABLED_DIR: this.disabled,
      BOT_NAME: this.bot,
      BOT_NAMESPACE: '6ba7b810-9dad-11d1-80b4-00c04fd430c8',
      TELEGRAM_BOT_TOKEN: TOKEN,
      DISPATCHER_PORT: String(this.port),
      DISPATCHER_TELEGRAM_API_BASE: this.fake.url,
      CLAUDE_BIN: join(this.base, 'stub', 'claude'),
      CLAUDEBOTLIFE_TEST: '1',
      CLAUDEBOTLIFE_TEST_ROOT: this.base,
      CLAUDEBOT_CONFIG_DIR: join(this.base, 'configs'),
      HUB_CONFIGS_DIR: join(this.base, 'configs'),
      CLAUDEBOT_GLOBAL_CFG: join(this.base, 'configs', '_global.yml'),
      CLAUDEBOT_SECRETS_FILE: join(this.base, 'configs', '_secrets.yml'),
      CLAUDEBOT_PROVIDER_PATH: join(this.base, 'providers.json'),
      CLAUDEBOT_CHANNELS_ROOT: join(this.base, 'channels'),
      ...(this.opts.extraEnv ?? {}),
    }
    const proc = Bun.spawn([process.execPath, 'run', 'dispatcher/dispatcher.ts'], { cwd: REPO, env, stdout: 'pipe', stderr: 'pipe' })
    this.proc = proc
    const pump = async (stream: ReadableStream<Uint8Array> | undefined, which: 'err' | 'out') => {
      if (!stream) return
      const dec = new TextDecoder()
      const reader = stream.getReader()
      try {
        while (true) {
          const { value, done } = await reader.read()
          if (done) break
          const s = dec.decode(value)
          if (which === 'err') this.errBuf += s; else this.outBuf += s
        }
      } catch { /* ignore */ }
    }
    void pump(proc.stderr as any, 'err')
    void pump(proc.stdout as any, 'out')
  }

  stderr(): string { return this.errBuf }
  /** 非 stderr 的输出，仅用于诊断 */
  stdout(): string { return this.outBuf }

  async waitStderr(pattern: string | RegExp, timeoutMs: number): Promise<string> {
    await waitFor(() => (typeof pattern === 'string' ? this.errBuf.includes(pattern) : pattern.test(this.errBuf)), timeoutMs,
      `stderr 出现 ${String(pattern)}`)
    return this.errBuf
  }

  async waitHttp(timeoutMs = 30000): Promise<void> {
    await waitFor(async () => (await fetch(`http://127.0.0.1:${this.port}/health`).then(r => r.ok || r.status > 0).catch(() => false)), timeoutMs, 'dispatcher HTTP 起来')
  }

  async health(): Promise<{ status: number; text: string }> {
    const r = await fetch(`http://127.0.0.1:${this.port}/health`)
    return { status: r.status, text: await r.text() }
  }

  async send(body: Record<string, unknown>, timeoutMs = 60000): Promise<{ status: number; json: any; text: string }> {
    const r = await fetch(`http://127.0.0.1:${this.port}/send`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${API_TOKEN}`, 'x-api-token': API_TOKEN },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    })
    const text = await r.text()
    let json: any = null
    try { json = JSON.parse(text) } catch { /* 非 JSON */ }
    return { status: r.status, json, text }
  }

  async post(path: string, body: Record<string, unknown>): Promise<{ status: number; json: any; text: string }> {
    const r = await fetch(`http://127.0.0.1:${this.port}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${API_TOKEN}`, 'x-api-token': API_TOKEN },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30000),
    })
    const text = await r.text()
    let json: any = null
    try { json = JSON.parse(text) } catch { /* 非 JSON */ }
    return { status: r.status, json, text }
  }

  /** CHANNEL_DIR/inbox 下所有能解析成 JSON 的件 */
  inboxItems(): { file: string; data: any }[] {
    const out: { file: string; data: any }[] = []
    const walk = (d: string) => {
      let es: string[] = []
      try { es = readdirSync(d) } catch { return }
      for (const e of es.sort()) {
        const full = join(d, e)
        let st; try { st = statSync(full) } catch { continue }
        if (st.isDirectory()) { walk(full); continue }
        try { out.push({ file: full, data: JSON.parse(readFileSync(full, 'utf8')) }) } catch { /* 非 JSON */ }
      }
    }
    walk(join(this.channel, 'inbox'))
    return out
  }

  resendStatePath(chat = PRIVATE_CHAT): string { return join(this.channel, '.resend', `${chat}.json`) }
  readResendState(chat = PRIVATE_CHAT): any | null {
    try { return JSON.parse(readFileSync(this.resendStatePath(chat), 'utf8')) } catch { return null }
  }
  linkStateFile(): string { return join(this.linkState, this.bot) }

  /**
   * 「判为不通」：假服务正常、出现 polling as 后切到断线，发一次触发用 /send（该段被断开记为状态不明），
   * 等 stderr 出现 link_down since=，再清空假服务收到的方法记录。
   */
  async makeLinkDown(timeoutMs = 60000): Promise<void> {
    await this.fake.setMode('cut')
    await this.send({ chat_id: PRIVATE_CHAT, text: '触发用' }, 30000).catch(() => null)
    await this.waitStderr('link_down since=', timeoutMs)
    this.fake.clearCalls()
  }

  async bringLinkUp(): Promise<void> { await this.fake.setMode('normal') }

  killNow(): void { try { this.proc?.kill(9) } catch { /* ignore */ } }

  async stop(): Promise<void> {
    live.delete(this)
    const p = this.proc
    if (p) {
      try { p.kill(15) } catch { /* ignore */ }
      const done = await Promise.race([p.exited.then(() => true), sleep(4000).then(() => false)])
      if (!done) { try { p.kill(9) } catch { /* ignore */ } await Promise.race([p.exited, sleep(2000)]) }
    }
    await this.fake.close().catch(() => { /* ignore */ })
    try { rmSync(this.base, { recursive: true, force: true }) } catch { /* ignore */ }
  }
}

/** afterEach 用：杀光本文件里起过的所有子进程并清理临时目录 */
export async function stopAll(): Promise<void> {
  const hs = [...live]
  for (const h of hs) await h.stop()
}

export async function startedHarness(opts: HarnessOpts = {}): Promise<Harness> {
  const h = await Harness.create(opts)
  h.start()
  return h
}

/** 起 dispatcher 并等到 polling as（假服务应为 normal） */
export async function startedPolling(opts: HarnessOpts = {}): Promise<Harness> {
  const h = await startedHarness(opts)
  await h.waitStderr('polling as', 30000)
  return h
}

export function pathExists(p: string): boolean { return existsSync(p) }
export function readTextOrNull(p: string): string | null { try { return readFileSync(p, 'utf8') } catch { return null } }
export function readBytesOrNull(p: string): Buffer | null { try { return readFileSync(p) } catch { return null } }
