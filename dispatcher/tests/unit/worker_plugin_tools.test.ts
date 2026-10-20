// T18 worker 插件工具面：黑盒起 MCP stdio 子进程，只依据 INTERFACE/PLAN 的工具与提示契约。
// 不连真 Telegram、不起真 worker；dispatcher 用本机假 HTTP。
import { afterAll, beforeAll, describe, expect, test } from 'bun:test'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const PLUGIN = join(import.meta.dir, '..', '..', 'worker-plugin.ts')
const dirs: string[] = []
let server: any
let base = ''
const seen: Array<{ path: string; body: any }> = []

beforeAll(() => {
  server = Bun.serve({
    port: 0,
    async fetch(req) {
      const u = new URL(req.url)
      const body = await req.json().catch(() => null)
      seen.push({ path: u.pathname, body })
      if (u.pathname === '/send') {
        return Response.json({ ok: true, planned: 1, sent: 1, dropped: [], message_ids: [9] })
      }
      if (u.pathname === '/party') {
        return Response.json({ ok: true, chat_id: body?.chat_id, party: { on: true, source: 'bot' } })
      }
      if (u.pathname === '/promise') {
        return Response.json({ ok: true, items: [] })
      }
      return Response.json({ ok: true })
    },
  })
  base = `http://127.0.0.1:${server.port}`
})
afterAll(() => {
  try { server?.stop(true) } catch {}
  for (const d of dirs) try { rmSync(d, { recursive: true, force: true }) } catch {}
})

type Session = { init?: any; tools?: any[]; call?: any; raw: any[] }
async function rpc(channel: string, opts: { list?: boolean; tool?: string; args?: any }): Promise<Session> {
  const proc = Bun.spawn([process.execPath, 'run', PLUGIN], {
    stdin: 'pipe', stdout: 'pipe', stderr: 'pipe',
    env: {
      ...process.env, CHANNEL_DIR: channel, TELEGRAM_WORKER_BOT: 't18bot',
      TELEGRAM_DISPATCHER_URL: base, DISPATCHER_URL: base, BOT_NAME: 't18bot',
      CLAUDEBOTLIFE_TEST: '1', CLAUDEBOTLIFE_TEST_ROOT: channel,
      HOME: channel, TELEGRAM_WORKER_CHAT_ID: '100001',
    },
  })
  const out: Session = { raw: [] }
  const dec = new TextDecoder()
  let buf = ''
  const waiters = new Map<number, (v: any) => void>()
  const pumps = (async () => {
    const reader = proc.stdout.getReader()
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += dec.decode(value)
        let nl
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1)
          if (!line) continue
          try {
            const msg = JSON.parse(line)
            if (msg.id != null) { out.raw.push(msg); waiters.get(msg.id)?.(msg) }
          } catch {}
        }
      }
    } catch {}
  })()
  const send = (o: any) => { try { proc.stdin.write(JSON.stringify(o) + '\n') } catch {} }
  const wait = (id: number) => new Promise<any>(resolve => {
    const t = setTimeout(() => resolve(undefined), 6000)
    waiters.set(id, v => { clearTimeout(t); resolve(v) })
  })
  send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 't18', version: '1' } } })
  send({ jsonrpc: '2.0', method: 'notifications/initialized' })
  out.init = await wait(1)
  if (opts.list) {
    send({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} })
    out.tools = (await wait(2))?.result?.tools
  }
  if (opts.tool) {
    send({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: opts.tool, arguments: opts.args ?? {} } })
    out.call = (await wait(3))?.result
  }
  try { proc.kill() } catch {}
  await pumps.catch(() => {})
  return out
}

function channel(files: Record<string, unknown>): string {
  const d = mkdtempSync(join(tmpdir(), 't18-plugin-'))
  dirs.push(d)
  writeFileSync(join(d, '.api-token'), 'TEST-TOKEN')
  for (const [name, value] of Object.entries(files)) {
    writeFileSync(join(d, name), typeof value === 'string' ? value : JSON.stringify(value))
  }
  return d
}
const textOf = (r: any) => (r?.content ?? []).map((c: any) => c.text ?? '').join('')

describe('工具列表与功能开关', () => {
  test('全开：reply 带语音参数，含群聊工具与三个承诺工具', async () => {
    const ch = channel({})
    const s = await rpc(ch, { list: true })
    const names = (s.tools ?? []).map((t: any) => t.name)
    expect(names).toEqual(['reply', 'react', 'download_attachment', 'add_group_alias', 'set_party_mode', 'promise_set', 'promise_cancel', 'promise_list'])
    const reply = (s.tools ?? []).find((t: any) => t.name === 'reply')
    expect(Object.keys(reply.inputSchema.properties)).toContain('as_voice')
    expect(reply.description).toContain('标记不要写进 voice_text')
    expect(String(s.init?.result?.instructions ?? '')).not.toContain('关掉了这些功能')
  })

  test('语音/群聊关闭：reply 去掉语音参数，群聊工具消失，说明里列出关闭项', async () => {
    const ch = channel({ 'features.json': { version: 1, default: true, features: { voice: false, groups: false } } })
    const s = await rpc(ch, { list: true })
    const names = (s.tools ?? []).map((t: any) => t.name)
    expect(names).not.toContain('add_group_alias')
    expect(names).not.toContain('set_party_mode')
    expect(names).toContain('promise_set')
    const reply = (s.tools ?? []).find((t: any) => t.name === 'reply')
    expect(Object.keys(reply.inputSchema.properties)).not.toContain('as_voice')
    expect(reply.description).not.toContain('voice_text')
    const ins = String(s.init?.result?.instructions ?? '')
    expect(ins).toContain('这只 bot 现在关掉了这些功能：')
    expect(ins).toContain('发语音')
    expect(ins).toContain('群聊')
  })
})

describe('party 与承诺工具接线', () => {
  test('set_party_mode 绑定当前投递件 message_id，成功文案一致', async () => {
    const ch = channel({
      '.last-chat-id': '-100123',
      '.last-delivery.json': { chat_id: '-100123', message_id: '77', party_chat_id: '', party_ambiguous: false, promise_ids: [], promise_due: false, human_chat_id: '-100123', human_user_id: '100001', at_ms: Date.now() + 60_000 },
      'access.json': { allowFrom: ['100001'], groups: { '-100123': {} } },
    })
    seen.length = 0
    const s = await rpc(ch, { tool: 'set_party_mode', args: { on: true, note: '线下' } })
    expect(s.call?.isError ?? false).toBe(false)
    expect(textOf(s.call)).toBe('party on: chat=-100123 source=bot')
    const req = seen.find(x => x.path === '/party')
    expect(req?.body).toMatchObject({ chat_id: '-100123', on: true, note: '线下', message_id: '77' })
  })

  test('promise_list 无真人投递 → chat_not_allowed，不发请求', async () => {
    const ch = channel({ '.last-chat-id': '100001' })
    seen.length = 0
    const s = await rpc(ch, { tool: 'promise_list', args: { chat_id: '100001' } })
    expect(s.call?.isError).toBe(true)
    expect(textOf(s.call)).toContain('chat_not_allowed')
    expect(seen.some(x => x.path === '/promise')).toBe(false)
  })

  test('promise_list 绑定当前聊天，请求体带 scope_chat_id', async () => {
    const ch = channel({
      '.last-chat-id': '100001',
      '.last-delivery.json': { chat_id: '100001', message_id: '5', party_chat_id: '', party_ambiguous: false, promise_ids: [], promise_due: false, human_chat_id: '100001', human_user_id: '100001', at_ms: Date.now() + 60_000 },
    })
    seen.length = 0
    const s = await rpc(ch, { tool: 'promise_list', args: { chat_id: '100001' } })
    expect(s.call?.isError ?? false).toBe(false)
    const req = seen.find(x => x.path === '/promise')
    expect(req?.body).toEqual({ action: 'list', scope_chat_id: '100001' })
  })
})

describe('party 改投下的 reply 路由', () => {
  test('无显式 chat_id 时发到 party 群，且不转发私聊的 reply_to', async () => {
    const ch = channel({
      '.last-chat-id': '100001',
      '.last-inbound-ts.json': { _human_by_chat: { '100001': Date.now() }, _director_by_chat: {}, _mentions_group_by_chat: {}, _human: Date.now() },
      '.last-delivery.json': { chat_id: '100001', message_id: '6', party_chat_id: '-100123', party_ambiguous: false, promise_ids: [], promise_due: false, human_chat_id: '100001', human_user_id: '100001', at_ms: Date.now() + 60_000 },
    })
    seen.length = 0
    const s = await rpc(ch, { tool: 'reply', args: { text: '好', reply_to: '6' } })
    expect(s.call?.isError ?? false).toBe(false)
    const req = seen.find(x => x.path === '/send')
    expect(req?.body.chat_id).toBe('-100123')
    expect(req?.body.reply_to).toBeUndefined()
  })
})
