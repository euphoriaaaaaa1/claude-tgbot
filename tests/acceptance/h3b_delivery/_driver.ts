// H3b 子进程驱动：为"必须经过消息处理路径"的场景（H-14 语音 / H-12 清空）提供
// 强隔离运行环境。由 _driverlib.ts 以子进程方式 spawn。
//
// 只依据 .devflow/INTERFACE-h3b-delivery.md。不读实现。
// 硬性隔离：api.telegram.org 与一切非 Telegram 出站都被改写到本机假服务；
//   PATH/CLAUDE_BIN 指向假 claude（exit 97），绝不真起进程；临时目录；假令牌。
//
// 用法：bun run _driver.ts <spec.json> <out.json>
// spec: { scenario:'voice'|'clear', updates:[...], waitMs?, transcribe?:{delayMs?,status?} }
// out:  { tgMethods:[], transcribe:[{path,body}], inboxFiles:[], stderr:[], order:[], events:[] }
import { mkdtempSync, mkdirSync, chmodSync, writeFileSync, readdirSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'

const specPath = process.argv[2]
const outPath = process.argv[3]
const spec: any = JSON.parse(readFileSync(specPath, 'utf8'))

const BASE = mkdtempSync(join(tmpdir(), 'h3b-drv-'))
const HOME = join(BASE, 'home')
const STUB = join(BASE, 'stub')
const CH = join(BASE, 'channel')
mkdirSync(HOME, { recursive: true }); mkdirSync(STUB, { recursive: true }); mkdirSync(CH, { recursive: true })
for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'schtasks', 'kill']) {
  const p = join(STUB, n); writeFileSync(p, `#!/bin/sh\nexit 97\n`); chmodSync(p, 0o755)
}
// 可控假 claude：ignore-term = 忽略 TERM/INT/HUP 并长睡（用于验证"exit 未到"与超时强制放行）；
// sleep-term = 长睡但响应 TERM（被 kill 后迅速进入 exit）。
if (spec.claudeStub === 'ignore-term') {
  const c = join(STUB, 'claude')
  writeFileSync(c, `#!/bin/sh\ntrap '' TERM INT HUP\nsleep 30\n`); chmodSync(c, 0o755)
} else if (spec.claudeStub === 'sleep-term') {
  const c = join(STUB, 'claude')
  writeFileSync(c, `#!/bin/sh\nsleep 30\n`); chmodSync(c, 0o755)
}
writeFileSync(join(CH, 'access.json'), JSON.stringify(spec.access ?? { allowFrom: ['123456789'], groups: {} }))
process.env.CHANNEL_DIR = CH
process.env.HOME = HOME
process.env.PATH = `${STUB}:${process.env.PATH ?? ''}`
process.env.CLAUDE_BIN = join(STUB, 'claude')
process.env.CLAUDEBOTLIFE_TEST = '1'
process.env.CLAUDEBOTLIFE_TEST_ROOT = BASE
process.env.BOT_NAME = 'h3bdrv'
process.env.BOT_NAMESPACE = '6ba7b810-9dad-11d1-80b4-00c04fd430c8'
process.env.TELEGRAM_BOT_TOKEN = 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK'
process.env.DISPATCHER_PORT = '18991'
process.env.TELEGRAM_DISPATCHER_URL = 'http://127.0.0.1:18991'
process.env.DISPATCHER_URL = 'http://127.0.0.1:18991'

// ─── 观察记录 ────────────────────────────────────────────────────────────
const res: any = { tgMethods: [], transcribe: [], inboxFiles: [], stderr: [], order: [], events: [] }
const flush = () => { try { writeFileSync(outPath, JSON.stringify(res)) } catch { /* ignore */ } }
const errs: string[] = []
const oe = process.stderr.write.bind(process.stderr)
;(process.stderr as any).write = (s: any, ...a: any[]) => { errs.push(String(s)); res.stderr = errs.slice(); flush(); return oe(s, ...a) }

// ─── 假 Telegram ────────────────────────────────────────────────────────
const updates: any[] = spec.updates ?? []
let upIdx = 0
const tg = Bun.serve({
  port: 0,
  async fetch(req) {
    const u = new URL(req.url)
    const method = u.pathname.split('/').filter(Boolean).pop() ?? ''
    res.tgMethods.push(method); flush()
    if (method === 'getUpdates') {
      const one = upIdx < updates.length ? [updates[upIdx++]] : []
      return Response.json({ ok: true, result: one })
    }
    if (method === 'getMe') return Response.json({ ok: true, result: { id: 1, is_bot: true, username: 'h3bdrv', first_name: 'p' } })
    if (method === 'deleteWebhook') return Response.json({ ok: true, result: true })
    if (method === 'getFile') return Response.json({ ok: true, result: { file_id: 'VF1', file_unique_id: 'u1', file_size: 50, file_path: 'voice/v.ogg' } })
    if (u.pathname.includes('/file/')) {
      if (spec.downloadDelayMs) await new Promise(r => setTimeout(r, spec.downloadDelayMs))
      return new Response(Buffer.from('FAKEAUDIOBYTES'), { status: 200 })
    }
    return Response.json({ ok: true, result: { message_id: 111, chat: { id: 1 }, date: 0 } })
  },
})
const TG_ROOT = `http://127.0.0.1:${tg.port}`

// ─── 假转写服务 ──────────────────────────────────────────────────────────
const trOpts = spec.transcribe ?? {}
const tr = Bun.serve({
  port: 0,
  async fetch(req) {
    const u = new URL(req.url)
    let body = ''
    try { body = await req.text() } catch { /* ignore */ }
    res.transcribe.push({ path: u.pathname, body: body.slice(0, 800) }); flush()
    if (trOpts.delayMs) await new Promise(r => setTimeout(r, trOpts.delayMs))
    if (trOpts.status) return new Response('err', { status: trOpts.status })
    return Response.json({ text: trOpts.text ?? '转写结果' })
  },
})
const TR_ROOT = `http://127.0.0.1:${tr.port}`

// ─── 网络改写（绝不外发）────────────────────────────────────────────────
const rewrite = (url: string) => url.includes('api.telegram.org')
  ? url.replace(/https?:\/\/api\.telegram\.org/, TG_ROOT)
  : TR_ROOT + '/' + (url.replace(/^https?:\/\//, '').split('/').slice(1).join('/'))
const nf: any = createRequire(import.meta.url)('node-fetch')
if (nf && typeof nf.default === 'function') { const o = nf.default; nf.default = (u: any, opt: any) => o(rewrite(String(u)), opt) }
const rawFetch = globalThis.fetch.bind(globalThis) // 未被改写：仅供驱动自己访问本机 dispatcher
const of = rawFetch
globalThis.fetch = ((i: any, init?: any) => {
  const url = typeof i === 'string' ? i : (i?.url ?? String(i))
  const next = rewrite(url)
  return next !== url ? of(next, init) : of(i, init)
}) as typeof fetch

// ─── 可选：预置会话文件（用于观察清空时"移动会话文件"的时序）──────────────
let sessionDir = ''
let sessionPath = ''
if (spec.preSession) {
  const wm: any = await import('../../../dispatcher/worker-manager')
  const slug = wm.projectSlug(CH)
  const uuid = wm.unifiedSessionUuid(String(spec.sessionChat ?? '123456789'))
  sessionDir = join(HOME, '.claude', 'projects', slug)
  mkdirSync(sessionDir, { recursive: true })
  sessionPath = join(sessionDir, uuid + '.jsonl')
  writeFileSync(sessionPath, '{"placeholder":true}\n')
}

// ─── 运行 ────────────────────────────────────────────────────────────────
await import('../../../dispatcher/dispatcher')

// 可选：驱动自己调用本机 dispatcher 的 /download，观测"图片下载超时"是否封顶返回并记 download_timeout。
if (spec.runDownload) {
  const token = readFileSync(join(CH, '.api-token'), 'utf8').trim()
  const ctl = new AbortController()
  const k = setTimeout(() => ctl.abort(), spec.downloadAbortMs ?? 22000)
  const dt0 = Date.now()
  let returned = false, status = 0, body: any = null
  try {
    const r = await rawFetch('http://127.0.0.1:18991/download', {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-api-token': token },
      body: JSON.stringify({ file_id: 'VF1', chat_type: 'private' }), signal: ctl.signal,
    })
    status = r.status; try { body = await r.json() } catch { /* ignore */ }; returned = true
  } catch { returned = false }
  clearTimeout(k)
  res.download = { returned, elapsed: Date.now() - dt0, status, body }
  flush()
}

const waitMs = spec.waitMs ?? 3500
const t0 = Date.now()
while (Date.now() - t0 < waitMs) {
  try { res.inboxFiles = readdirSync(join(CH, 'inbox')) } catch { res.inboxFiles = [] }
  flush()
  await new Promise(r => setTimeout(r, 100))
}
if (sessionDir) {
  let siblings: string[] = []
  try { siblings = readdirSync(sessionDir) } catch { /* ignore */ }
  res.session = { originalExists: sessionPath ? existsSync(sessionPath) : false, siblings }
}
res.events = (spec.events ?? [])
flush()
try { tr.stop(true) } catch { /* ignore */ }
try { tg.stop(true) } catch { /* ignore */ }
// 兜底：若被 spawn 的子进程让事件循环无法正常退出，1s 后强制杀死自己（SIGKILL 不可被拦截）。
setTimeout(() => { try { process.kill(process.pid, 'SIGKILL') } catch { /* ignore */ } }, 1000)
process.exit(0)
