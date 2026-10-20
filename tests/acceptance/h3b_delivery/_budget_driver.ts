// H-17 总耗时上界 —— 独立子进程驱动（慢测隔离）
// 让 sendMessage 永不返回：若实现有 SEND_TOTAL_BUDGET_MS 上界，请求应在界内返回；
// 否则会一直挂住。把这段"挂住"放进子进程，避免污染同进程的其它测试文件。
// 用法：bun run _budget_driver.ts <out.json>
import { mkdtempSync, mkdirSync, chmodSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'

const outPath = process.argv[2]
const BASE = mkdtempSync(join(tmpdir(), 'h3b-budget-'))
const HOME = join(BASE, 'home'), STUB = join(BASE, 'stub'), CH = join(BASE, 'channel')
mkdirSync(HOME, { recursive: true }); mkdirSync(STUB, { recursive: true }); mkdirSync(CH, { recursive: true })
for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'schtasks', 'kill']) {
  const p = join(STUB, n); writeFileSync(p, `#!/bin/sh\nexit 97\n`); chmodSync(p, 0o755)
}
writeFileSync(join(CH, 'access.json'), JSON.stringify({ allowFrom: ['123456789'], groups: {} }))
Object.assign(process.env, {
  CHANNEL_DIR: CH, HOME, PATH: `${STUB}:${process.env.PATH ?? ''}`, CLAUDE_BIN: join(STUB, 'claude'),
  CLAUDEBOTLIFE_TEST: '1', CLAUDEBOTLIFE_TEST_ROOT: BASE, BOT_NAME: 'h3bbud',
  BOT_NAMESPACE: '6ba7b810-9dad-11d1-80b4-00c04fd430c8', TELEGRAM_BOT_TOKEN: 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK',
  DISPATCHER_PORT: '18991', TELEGRAM_DISPATCHER_URL: 'http://127.0.0.1:18991', DISPATCHER_URL: 'http://127.0.0.1:18991',
})

const tg = Bun.serve({
  port: 0,
  async fetch(req) {
    const u = new URL(req.url); const method = u.pathname.split('/').filter(Boolean).pop() ?? ''
    if (u.pathname.includes('/file/')) return new Response(Buffer.from('FAKE'), { status: 200 })
    if (method === 'getUpdates') return Response.json({ ok: true, result: [] })
    if (method === 'getMe') return Response.json({ ok: true, result: { id: 1, is_bot: true, username: 'h3bbud', first_name: 'p' } })
    if (method === 'deleteWebhook') return Response.json({ ok: true, result: true })
    if (method === 'sendMessage') return new Promise<Response>(() => { /* 永不返回 */ })
    return Response.json({ ok: true, result: { message_id: 1, chat: { id: 1 }, date: 0 } })
  },
})
const TG_ROOT = `http://127.0.0.1:${tg.port}`
const nf: any = createRequire(import.meta.url)('node-fetch')
if (nf && typeof nf.default === 'function') { const o = nf.default; nf.default = (u: any, oo: any) => o(String(u).includes('api.telegram.org') ? String(u).replace(/https?:\/\/api\.telegram\.org/, TG_ROOT) : String(u), oo) }
const of = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((i: any, init?: any) => {
  const url = typeof i === 'string' ? i : (i?.url ?? String(i))
  return url.includes('api.telegram.org') ? of(url.replace(/https?:\/\/api\.telegram\.org/, TG_ROOT), init) : of(i, init)
}) as typeof fetch

const mod: any = await import('../../../dispatcher/dispatcher')
const srv: any = await mod.createApiServer({ port: 0 })
const s = srv?.server ?? srv; const port = Number(s?.port ?? srv?.port)
const { readFileSync } = await import('node:fs')
const token = readFileSync(join(CH, '.api-token'), 'utf8').trim()
const img = join(BASE, 'x.png'); writeFileSync(img, Buffer.from('89504e470d0a1a0a', 'hex'))

const ctl = new AbortController()
const killer = setTimeout(() => ctl.abort(), 66000)
const t0 = Date.now()
let returned = false, status = 0, body: any = null
try {
  const r = await fetch(`http://127.0.0.1:${port}/send`, {
    method: 'POST', headers: { 'content-type': 'application/json', 'x-api-token': token },
    body: JSON.stringify({ chat_id: '123456789', text: '段A', files: [img] }),
    signal: ctl.signal,
  })
  status = r.status
  try { body = JSON.parse(await r.text()) } catch { body = null }
  returned = true
} catch { returned = false }
clearTimeout(killer)
writeFileSync(outPath, JSON.stringify({ returned, elapsed: Date.now() - t0, status, body }))
try { s?.stop?.(true) } catch { /* ignore */ } try { tg.stop(true) } catch { /* ignore */ }
process.exit(0)
