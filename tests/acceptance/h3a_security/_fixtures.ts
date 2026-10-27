// H3a 三条安全项 —— 黑盒验收共享夹具
//
// 只依据 .devflow/{BRIEF,INTERFACE}-h3a-security.md 的对外契约。不读实现、不看 git 历史。
// 硬性隔离：绝不发真 Telegram 请求、绝不碰真实通道目录、绝不用真实令牌。
//   - CHANNEL_DIR / HOME 指向 mkdtemp 临时目录
//   - PATH 前置假桩（claude/tmux/... 一律 exit 97），即便实现无视注入也打不到真进程
//   - TELEGRAM_BOT_TOKEN 为明显假值；API 根指向本机死地址（若实现读得到）
//
// 契约未固定的对外接口，本夹具按下列"待实现接口假设"取值（写在报告里，供实现方对齐）：
//   A1 worker-manager.ts 导出 workerEnv(source)->Record<...>（候选名见 WORKER_ENV_NAMES）
//   A2 dispatcher.ts 导出 authorizeClear(ctx)（候选名见 CLEAR_FN_NAMES），返回 boolean 或 {ok|allowed|authorized}
//   A3 dispatcher.ts 导出可独立启动的本机 HTTP 服务工厂（候选名见 SERVER_FACTORY_NAMES），
//      在 port 0 上起服务，返回 Bun server（含 .port/.stop）或 {server,port}；启动时生成 <CHANNEL_DIR>/.api-token
//   A4 口令经请求头携带，接受 x-api-token / x-api-key / authorization: Bearer 之一
//   A5 bot 用户名取自 env TELEGRAM_BOT_USERNAME 或 ctx.me.username
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// ─── 身份常量 & 契约规定的通道夹具 ────────────────────────────────────────
export const OWNER = '100001'          // 私聊名单里的主人
export const NONOWNER = '100002'       // 不在任何名单
export const GROUP = '-100999000111'   // 群
export const BOT_USERNAME = 'h3asecbot'

// ─── 隔离环境（必须在任何被测模块 import 前设好）────────────────────────
export const BASE = mkdtempSync(join(tmpdir(), 'h3a-sec-'))
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
// 私聊名单=allowFrom，群名单=groups（本仓既有 access.json 形状）
writeFileSync(join(CHANNEL_DIR, 'access.json'),
  JSON.stringify({ allowFrom: [OWNER], groups: { [GROUP]: { title: '测试群' } } }))

// 明显假令牌 + 本机死地址：任何误发的 Telegram 调用只会打到 127.0.0.1 死端口
process.env.CHANNEL_DIR = CHANNEL_DIR
process.env.BOT_NAME ??= 'h3a-sec'
process.env.HOME = HOME
process.env.PATH = `${STUB}:${process.env.PATH ?? ''}`
process.env.CLAUDEBOTLIFE_TEST = '1'
process.env.CLAUDEBOTLIFE_TEST_ROOT = BASE
process.env.TELEGRAM_BOT_TOKEN = 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK'
process.env.TELEGRAM_BOT_USERNAME = BOT_USERNAME
process.env.TELEGRAM_WORKER_BOT = 'h3a-sec'
process.env.TELEGRAM_WORKER_CHAT_ID = OWNER
process.env.TELEGRAM_DISPATCHER_URL = 'http://127.0.0.1:9'
process.env.DISPATCHER_URL = process.env.TELEGRAM_DISPATCHER_URL
process.env.TELEGRAM_API_ROOT = 'http://127.0.0.1:9'   // 万一实现读它，也打不到真 Telegram

// ─── 共享临时目录的生命周期 ───────────────────────────────────────────────
// Bun 在单进程内跑完全部测试文件，共享同一模块实例 → BASE 是三个文件共用的。
// 各文件在 afterAll 里调 cleanupBase()。若运行期真删，先结束的文件会把共享目录
// 拆掉，后面文件全崩（实测 10/24）。故 cleanupBase() 运行期不真删，只在进程
// exit 时统一删除；BASE 在整轮测试内始终存活。
let exiting = false
function removeBase(): void {
  try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ }
}
/** 运行期调用不删除共享目录（惰性）；仅在进程退出阶段才真正清理。 */
export function cleanupBase(): void {
  if (exiting) removeBase()
}
process.on('exit', () => { exiting = true; removeBase() })

// ─── 动态加载被测模块（惰性；env 已在上方定死）──────────────────────────
export async function loadWorkerManager(): Promise<any> {
  const mod: any = await import('../../../dispatcher/worker-manager')
  return mod?.default ?? mod
}
export async function loadDispatcher(): Promise<any> {
  const mod: any = await import('../../../dispatcher/dispatcher')
  return mod?.default ?? mod
}

export const WORKER_ENV_NAMES = ['workerEnv', 'buildWorkerEnv', 'workerEnvironment', 'makeWorkerEnv']
export const CLEAR_FN_NAMES = ['authorizeClear', 'canClear', 'clearAuthorized', 'isClearAuthorized']
export const SERVER_FACTORY_NAMES = [
  'createApiServer', 'startApiServer', 'createHttpServer', 'startHttpServer',
  'makeApiServer', 'createServer', 'startServer', 'apiServer',
]
export const REQUIRED_WORKER_ENV = [
  'PATH', 'HOME', 'CHANNEL_DIR', 'TELEGRAM_WORKER_CHAT_ID', 'TELEGRAM_WORKER_BOT',
  'TELEGRAM_DISPATCHER_URL', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN',
]
export const OPTIONAL_WORKER_ENV = [
  'LANG', 'LC_ALL', 'TERM', 'TMPDIR', 'USER', 'SHELL', 'NO_PROXY', 'no_proxy',
  'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
]
export const FORBIDDEN_WORKER_ENV = ['TELEGRAM_BOT_TOKEN', 'PEERS_JSON', 'ANTHROPIC_API_KEY', 'TOP_SECRET_UNLISTED']

/** 从模块里按候选名取第一个函数；没有则返回 undefined。 */
export function pickFn(mod: any, names: string[]): any {
  for (const n of names) if (typeof mod?.[n] === 'function') return mod[n]
  return undefined
}

/** 把授权返回值归一化为布尔：true / {ok|allowed|authorized:true} 视为放行。 */
export function allowed(r: any): boolean {
  if (typeof r === 'boolean') return r
  if (r && typeof r === 'object') return r.ok === true || r.allowed === true || r.authorized === true
  return false
}

// ─── 本机 HTTP 服务夹具 ───────────────────────────────────────────────────
export interface TestServer { base: string; port: number; token: string; stop: () => void }

/** 取本机 HTTP 服务工厂（找不到返回 undefined）。 */
export async function getServerFactory(): Promise<any> {
  const mod = await loadDispatcher()
  return pickFn(mod, SERVER_FACTORY_NAMES)
}

/** 起一个本机临时 HTTP 服务；找不到工厂返回 null（调用方据 null 判失败）。 */
export async function startTestServer(): Promise<TestServer | null> {
  const f = await getServerFactory()
  if (!f) return null
  const attempts: any[] = [() => f({ port: 0 }), () => f(0), () => f(), () => f({ port: 0, channelDir: CHANNEL_DIR })]
  for (const call of attempts) {
    let r: any
    try { r = await call() } catch { continue }
    const srv = r?.server ?? r
    const port = Number(srv?.port ?? r?.port ?? srv?.address?.port ?? 0)
    if (port > 0 && typeof srv?.stop === 'function') {
      const stop = () => { try { srv.stop(true) } catch { /* ignore */ } }
      return { base: `http://127.0.0.1:${port}`, port, token: readToken(), stop }
    }
    // Bun.serve 的返回值本身也可当句柄
    if (typeof r?.stop === 'function' && Number(r?.port) > 0) {
      const port2 = Number(r.port)
      return { base: `http://127.0.0.1:${port2}`, port: port2, token: readToken(), stop: () => { try { r.stop(true) } catch { /* ignore */ } } }
    }
    if (typeof r?.stop === 'function') { try { r.stop(true) } catch { /* ignore */ } }
  }
  return null
}

export function tokenPath(): string { return join(CHANNEL_DIR, '.api-token') }
export function readToken(): string {
  try { return readFileSync(tokenPath(), 'utf8') } catch { return '' }
}
export function removeToken(): void { try { rmSync(tokenPath(), { force: true }) } catch { /* ignore */ } }

/** 带正确口令的请求头（同时给三种常见头名，避免依赖具体头名）。 */
export function tokenHeaders(token: string | undefined): Record<string, string> {
  if (!token) return {}
  return { 'x-api-token': token, 'x-api-key': token, authorization: `Bearer ${token}` }
}
/** 带错误口令的请求头。 */
export function wrongHeaders(): Record<string, string> {
  const bad = 'WRONG-' + 'A'.repeat(40)
  return { 'x-api-token': bad, 'x-api-key': bad, authorization: `Bearer ${bad}` }
}

/** 递归列出目录下的相对路径（用于"零副作用"快照）。 */
export function snapshotDir(dir: string): string[] {
  const out: string[] = []
  const walk = (d: string, rel: string) => {
    let es: string[] = []
    try { es = readdirSync(d) } catch { return }
    for (const e of es.sort()) {
      const full = join(d, e), r = rel ? `${rel}/${e}` : e
      out.push(r)
      walk(full, r)
    }
  }
  walk(dir, '')
  return out
}

export { existsSync }
