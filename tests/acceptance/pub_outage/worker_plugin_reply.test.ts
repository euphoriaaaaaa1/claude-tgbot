// pub-outage · worker 的 reply 工具对 resend_parts 的处理（行为清单 #21）
// 手法：起一个返回固定回包的本机假 dispatcher，用 MCP stdio 调 worker-plugin 的 reply 工具。
// 只依据 pub-outage-接口.md 6.4 / 6.4a / 6.5。绝不连真实 dispatcher 端口。
import { test, expect, beforeAll, afterAll } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

for (const k of Object.keys(process.env)) if (/_proxy$/i.test(k)) delete process.env[k]
process.env.NO_PROXY = '*'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..', '..', '..'), 'dispatcher', 'worker-plugin.ts')

let CH = ''
let fake: ReturnType<typeof Bun.serve> | null = null
let reply: { status: number; raw: string } = { status: 200, raw: '{}' }

beforeAll(() => {
  CH = mkdtempSync(join(tmpdir(), 'pubout-plug-'))
  writeFileSync(join(CH, '.api-token'), 'TEST-TOKEN-123')
  fake = Bun.serve({
    port: 0,
    hostname: '127.0.0.1',
    async fetch(req) {
      const u = new URL(req.url)
      if (u.pathname === '/send') return new Response(reply.raw, { status: reply.status, headers: { 'content-type': 'application/json' } })
      return Response.json({ ok: true })
    },
  })
})
afterAll(() => {
  try { fake?.stop(true) } catch { /* ignore */ }
  try { rmSync(CH, { recursive: true, force: true }) } catch { /* ignore */ }
})

async function callReply(status: number, body: unknown): Promise<string> {
  const raw = typeof body === 'string' ? body : JSON.stringify(body)
  reply = { status, raw }
  const base = `http://127.0.0.1:${fake!.port}`
  const env: Record<string, string> = {
    PATH: process.env.PATH ?? '', HOME: CH, CHANNEL_DIR: CH, TELEGRAM_WORKER_BOT: 'outplug', TELEGRAM_WORKER_CHAT_ID: '123456789',
    TELEGRAM_DISPATCHER_URL: base, DISPATCHER_URL: base, BOT_NAME: 'outplug', NO_PROXY: '*',
  }
  const proc = Bun.spawn([process.execPath, 'run', PLUGIN], { stdin: 'pipe', stdout: 'pipe', stderr: 'ignore', env })
  const dec = new TextDecoder()
  const responses = new Map<number, any>()
  let buf = ''
  const reader = proc.stdout.getReader()
  const pump = (async () => {
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += dec.decode(value)
        let nl
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1)
          if (!line) continue
          try { const msg = JSON.parse(line); if (msg.id != null) responses.set(msg.id, msg) } catch { /* 非 JSON */ }
        }
      }
    } catch { /* ignore */ }
  })()
  const send = (o: any) => { try { proc.stdin.write(JSON.stringify(o) + '\n') } catch { /* ignore */ } }
  const waitId = async (id: number, ms: number) => {
    const end = Date.now() + ms
    while (!responses.has(id) && Date.now() < end) await new Promise(r => setTimeout(r, 50))
    return responses.get(id)
  }
  try {
    send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'pubout', version: '1' } } })
    send({ jsonrpc: '2.0', method: 'notifications/initialized' })
    await waitId(1, 10000)
    send({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'reply', arguments: { text: '正文占位', chat_id: '123456789' } } })
    const resp = await waitId(2, 15000)
    if (!resp) throw new Error('worker-plugin 没有回应 tools/call')
    const content = resp.result?.content
    if (Array.isArray(content)) return content.map((c: any) => c.text ?? '').join('')
    return String(resp.error?.message ?? JSON.stringify(resp))
  } finally {
    try { proc.kill() } catch { /* ignore */ }
    try { proc.stdin.end() } catch { /* ignore */ }
    await pump.catch(() => { /* ignore */ })
  }
}

const SCHEDULED = (nums: string) => `resend_scheduled: 段 ${nums} 确定没有发出，已登记补发；不要现在重发，稍后会收到系统提醒再处理。`
const PARTIAL = { ok: true, planned: 2, sent: 1, dropped: [{ index: 1, reason: 'link_down' }], message_ids: [111] }

test('reply_200部分送达且带resend_parts_原部分送达行之后多一行resend_scheduled', async () => {
  const base = await callReply(200, PARTIAL)
  expect(base).toContain('partial send: 1/2 delivered; missing segment(s) = 1')
  const withParts = await callReply(200, { ...PARTIAL, resend_parts: [1] })
  expect(withParts).toBe(`${base}\n${SCHEDULED('1')}`)
}, 40000)

test('reply_502且带resend_parts_原错误文本之后多一行resend_scheduled', async () => {
  const body = { ok: false, planned: 1, sent: 0, dropped: [{ index: 0, reason: 'link_down' }], resend_parts: [0] }
  const raw = JSON.stringify(body)
  const text = await callReply(502, raw)
  expect(text).toContain(`dispatcher /send 502: ${raw}`)
  expect(text.endsWith(`\n${SCHEDULED('0')}`)).toBe(true)
}, 40000)

test('reply_不带resend_parts_200部分送达与502_都没有resend_scheduled行', async () => {
  const partial = await callReply(200, PARTIAL)
  expect(partial).toContain('partial send: 1/2 delivered; missing segment(s) = 1')
  expect(partial).not.toContain('resend_scheduled')
  const raw = JSON.stringify({ ok: false, planned: 1, sent: 0, dropped: [{ index: 0, reason: 'link_down' }] })
  const failed = await callReply(502, raw)
  expect(failed).toContain(`dispatcher /send 502: ${raw}`)
  expect(failed).not.toContain('resend_scheduled')
}, 40000)

test('reply_resend_parts含负数或非整数或不是数组_忽略该字段_文本与不带时逐字相同', async () => {
  const base = await callReply(200, PARTIAL)
  for (const bad of [[-1, 'x'], [1.5], 'abc', { a: 1 }]) {
    const text = await callReply(200, { ...PARTIAL, resend_parts: bad })
    expect([JSON.stringify(bad), text]).toEqual([JSON.stringify(bad), base])
  }
}, 60000)
