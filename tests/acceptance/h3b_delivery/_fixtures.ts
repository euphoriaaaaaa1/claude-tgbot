// H3b 发送可靠性与时序 —— 黑盒验收共享夹具（进程内）
//
// 只依据 .devflow/{BRIEF,INTERFACE}-h3b-delivery.md 的对外契约。不读实现、不看 git 历史。
// 硬性隔离：绝不发真 Telegram 请求、绝不碰真实通道目录、绝不用真令牌、绝不真起 claude。
//   - node-fetch / globalThis.fetch 均被改写：api.telegram.org → 本机假 Telegram 服务
//   - PATH 前置假桩、CLAUDE_BIN 指向假 claude（exit 97），即便实现无视注入也打不到真进程
//   - CHANNEL_DIR / HOME 指向 mkdtemp 临时目录；TELEGRAM_BOT_TOKEN 为明显假值
//
// 契约未固定、由"运行时公共面"确认的对外接口（写在 TEST-PLAN 里，供实现方对齐）：
//   A1 worker-manager 导出 stripThinking(path, mode) / compactSessionIfHuge(path)（与既有测试同源）
//   A2 dispatcher 导出 createApiServer({port}) → {server|.stop, port}，本机 HTTP API
//   A3 /send 请求体：{ chat_id, text?, files?: string[], reply_to?: number, batch_id?: string }
//      响应体（修复后应恒含）：{ planned, sent, dropped:[{index,reason}], idempotent }
//   A4 备份文件与源文件同目录，形如 <file>.bak.<epoch>.<tag>.strip（集合差集判定"有无新备份"）
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'

export const OWNER = '123456789'
export const NONOWNER = '100002'
export const GROUP = '-100999000111'
export const BOT_NAME = 'h3bdeliv'
export const BOT_NAMESPACE = '6ba7b810-9dad-11d1-80b4-00c04fd430c8'
export const BOT_TOKEN = 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK'
export const ALLOWED_DISPATCHER_URL = 'http://127.0.0.1:18991' // 非禁用端口，避免 test_mode 守卫退出

// ─── 隔离环境（必须在任何被测模块 import 前设好）──────────────────────────
export const BASE = mkdtempSync(join(tmpdir(), 'h3b-delivery-'))
const HOME = join(BASE, 'home')
const STUB = join(BASE, 'stub')
export const CHANNEL_DIR = join(BASE, 'channel')
mkdirSync(HOME, { recursive: true })
mkdirSync(STUB, { recursive: true })
mkdirSync(CHANNEL_DIR, { recursive: true })
for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'schtasks', 'kill']) {
  const p = join(STUB, n)
  writeFileSync(p, `#!/bin/sh\necho "test_mode: real ${n} called" >&2\nexit 97\n`)
  chmodSync(p, 0o755)
}
writeFileSync(join(CHANNEL_DIR, 'access.json'),
  JSON.stringify({ allowFrom: [OWNER], groups: { [GROUP]: { title: '测试群' } } }))

process.env.CHANNEL_DIR = CHANNEL_DIR
process.env.HOME = HOME
process.env.PATH = `${STUB}:${process.env.PATH ?? ''}`
process.env.CLAUDE_BIN = join(STUB, 'claude')
process.env.CLAUDEBOTLIFE_TEST = '1'
process.env.CLAUDEBOTLIFE_TEST_ROOT = BASE
process.env.BOT_NAME = BOT_NAME
process.env.BOT_NAMESPACE = BOT_NAMESPACE
process.env.TELEGRAM_BOT_TOKEN = BOT_TOKEN
process.env.DISPATCHER_PORT = '18991'
process.env.TELEGRAM_DISPATCHER_URL = ALLOWED_DISPATCHER_URL
process.env.DISPATCHER_URL = ALLOWED_DISPATCHER_URL

// ─── 假 Telegram 服务（记录调用 + 可编程行为）─────────────────────────────
export interface TgCall { method: string; body: any }
const tgCalls: TgCall[] = []
let tgHandler: (method: string, body: any) => Response = () => json({ ok: true, result: { message_id: 1, chat: { id: 1 }, date: 0 } })

const tgServer = Bun.serve({
  port: 0,
  async fetch(req) {
    const u = new URL(req.url)
    const method = u.pathname.split('/').filter(Boolean).pop() ?? ''
    let body: any = null
    try { body = await req.clone().json() } catch { /* multipart/表单：略 */ }
    tgCalls.push({ method, body })
    if (u.pathname.includes('/file/')) return new Response(Buffer.from('FAKEFILEBYTES'), { status: 200 })
    return tgHandler(method, body)
  },
})
export const TG_ROOT = `http://127.0.0.1:${tgServer.port}`

function json(o: unknown): Response { return new Response(JSON.stringify(o), { status: 200, headers: { 'content-type': 'application/json' } }) }

export function tgCallsOf(method: string): TgCall[] { return tgCalls.filter(c => c.method === method) }
export function tgCount(method: string): number { return tgCalls.filter(c => c.method === method).length }
export function tgReset(): void { tgCalls.length = 0 }
export function tgSetHandler(h: (method: string, body: any) => Response): void { tgHandler = h }

/** 标准"全部成功"行为：getMe/getUpdates/deleteWebhook 骨架 + 发送成功。 */
export function tgDefaultHandler(): void {
  tgHandler = (method) => {
    if (method === 'getUpdates') return json({ ok: true, result: [] })
    if (method === 'getMe') return json({ ok: true, result: { id: 1, is_bot: true, username: BOT_NAME, first_name: 'p' } })
    if (method === 'deleteWebhook') return json({ ok: true, result: true })
    return json({ ok: true, result: { message_id: 1, chat: { id: 1 }, date: 0 } })
  }
}
tgDefaultHandler()

// ─── 改写网络：api.telegram.org → 假 Telegram（绝不外发）────────────────────
const __require = createRequire(import.meta.url)
try {
  const nf: any = __require('node-fetch')
  if (nf && typeof nf.default === 'function') {
    const orig = nf.default
    nf.default = (u: any, o: any) => orig(rewriteTg(String(u)), o)
  }
} catch { /* 无 node-fetch 时仅靠 globalThis.fetch */ }
export function rewriteTg(url: string): string {
  return url.includes('api.telegram.org') ? url.replace(/https?:\/\/api\.telegram\.org/, TG_ROOT.replace(/^http:/, 'http:')) : url
}
const __origFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: any, init?: any) => {
  const url = typeof input === 'string' ? input : (input?.url ?? String(input))
  const next = rewriteTg(url)
  if (next !== url) return __origFetch(next, init)
  return __origFetch(input, init)
}) as typeof fetch

// ─── 动态加载被测模块（惰性；env 已在上方定死）───────────────────────────
export interface DispatcherServer { base: string; port: number; token: string; stop: () => void }

export async function startDispatcherServer(opts: Record<string, unknown> = {}): Promise<DispatcherServer> {
  const mod: any = await import('../../../dispatcher/dispatcher')
  const factory = mod.createApiServer
  if (typeof factory !== 'function') throw new Error('dispatcher 未导出 createApiServer')
  const r: any = await factory({ port: 0, ...opts })
  const srv = r?.server ?? r
  const port = Number(srv?.port ?? r?.port ?? 0)
  if (!(port > 0)) throw new Error('createApiServer 未返回可用端口')
  const token = readFileSync(join(CHANNEL_DIR, '.api-token'), 'utf8').trim()
  const stop = () => { try { srv?.stop?.(true) } catch { /* ignore */ } }
  return { base: `http://127.0.0.1:${port}`, port, token, stop }
}

// ─── 共享临时目录生命周期（Bun 单进程共享；运行期不真删）──────────────────
let exiting = false
function removeBase(): void { try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } }
export function cleanupBase(): void { if (exiting) removeBase() }
process.on('exit', () => { exiting = true; removeBase() })

export function snapshotDir(dir: string): string[] {
  const out: string[] = []
  const walk = (d: string, rel: string) => {
    let es: string[] = []
    try { es = readdirSync(d) } catch { return }
    for (const e of es.sort()) { const full = join(d, e), r = rel ? `${rel}/${e}` : e; out.push(r); walk(full, r) }
  }
  walk(dir, '')
  return out
}

/** 造一张真实存在的图片文件（内容为 Png 魔数，供 sendPhoto 读取）。
 *  必须落在 /send 的媒体白名单内（CHANNEL_DIR/media），否则会被安全闸丢弃。 */
export function makeImage(name: string): string {
  const dir = join(CHANNEL_DIR, 'media')
  mkdirSync(dir, { recursive: true })
  const p = join(dir, name)
  writeFileSync(p, Buffer.from('89504e470d0a1a0a', 'hex'))
  return p
}

export { existsSync, readFileSync, writeFileSync, mkdirSync, mkdtempSync }
