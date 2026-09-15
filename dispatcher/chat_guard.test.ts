// 白盒：chat_guard 公开移植的补充点 + worker-plugin 的 D7 调用点收口（验收用例之外）
import { test, expect } from 'bun:test'
import { ROOT, HOME, MIN } from './tests/acceptance/desync/_env.ts'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { crossChatDecision, inboxHumanMarks, applyInboundMarks, loadInboundState, pickSrcChat } from './chat_guard.ts'

const PRIV = '123456', GRP = '-1001234567890'
const NOW = Date.UTC(2026, 8, 14, 7, 0, 0)

test('idleMin 按已过去的整分钟取 floor：30s→0、90s→1、599s→9', () => {
  expect([30_000, 90_000, 599_000].map(d => crossChatDecision(PRIV, GRP, NOW - d, NOW).idleMin)).toEqual([0, 1, 9])
})

test('inboxHumanMarks：连续多个前导 @mention 都剥掉再判"群"', () => {
  expect(inboxHumanMarks('@a @群助手 你好', false, false, 1, NOW).mentions_group).toBeUndefined()
  expect(inboxHumanMarks('@a @b 去群里说', false, false, 1, NOW).mentions_group).toBe(true)
})

test('src 只随真人消息移动：私聊真人之后同伴在群里发两条合成 → src 仍是私聊', () => {
  let s = loadInboundState(null)
  s = applyInboundMarks(s, PRIV, { human_ts: new Date(NOW - MIN).toISOString() }, NOW)
  s = applyInboundMarks(s, GRP, { text: '[peer-inbound] x', is_bot_sender: true }, NOW)
  s = applyInboundMarks(s, GRP, { text: '[self-initiate] y' }, NOW)
  expect(pickSrcChat(s)).toBe(PRIV)
})

// ---------- D7：worker-plugin 调用点（真拉起 worker-plugin.ts，mock /send 在 port 0）----------
async function runReplies(calls: Array<Record<string, unknown>>): Promise<{ outs: string[]; sends: string[] }> {
  const ch = join(ROOT, 'channels', 'd7')
  mkdirSync(ch, { recursive: true })
  const T = Date.now()
  writeFileSync(join(ch, 'access.json'), JSON.stringify({ allowFrom: [PRIV], groups: { [GRP]: {} } }))
  writeFileSync(join(ch, '.last-chat-id'), PRIV)
  writeFileSync(join(ch, '.last-inbound-ts.json'), JSON.stringify({
    _human: T - 2 * MIN, _human_by_chat: { [PRIV]: T - 2 * MIN }, _director_by_chat: {}, _mentions_group_by_chat: { [PRIV]: T - 2 * MIN },
  }))
  const sends: string[] = []
  const mock = Bun.serve({ port: 0, fetch: async req => {
    sends.push(String((await req.json()).chat_id)); return Response.json({ ok: true, message_ids: [sends.length] })
  } })
  const url = `http://127.0.0.1:${mock.port}`
  const p = Bun.spawn(['bun', 'run', join(import.meta.dir, 'worker-plugin.ts')], {
    env: { ...process.env, CLAUDEBOTLIFE_TEST: '1', CLAUDEBOTLIFE_TEST_ROOT: ROOT, HOME, TELEGRAM_WORKER_BOT: 'bot2',
      CHANNEL_DIR: ch, TELEGRAM_DISPATCHER_URL: url, DISPATCHER_URL: url },
    stdin: 'pipe', stdout: 'pipe', stderr: 'pipe',
  })
  const rd = p.stdout.getReader(), dec = new TextDecoder()
  let buf = ''
  const rpc = async (id: number, method: string, params: unknown) => {
    p.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); p.stdin.flush()
    for (;;) {
      let i
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1)
        try { const m = JSON.parse(line); if (m.id === id) return m } catch {}
      }
      const { value, done } = await rd.read()
      if (done) throw new Error('worker-plugin exited')
      buf += dec.decode(value)
    }
  }
  try {
    await rpc(1, 'initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'd7', version: '0' } })
    p.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n')
    const outs: string[] = []
    for (const [k, a] of calls.entries()) outs.push((await rpc(k + 2, 'tools/call', { name: 'reply', arguments: a })).result?.content?.[0]?.text)
    return { outs, sends }
  } finally { p.kill(); mock.stop(true) }
}

test('D7：私聊回复误带 user_requested 不烧额度 → 随后真去群里说仍放行', async () => {
  const r = await runReplies([
    { text: '私聊回你', chat_id: PRIV, user_requested: true },
    { text: '群里说', chat_id: GRP, user_requested: true },
    { text: '再发一次', chat_id: GRP, user_requested: true },
  ])
  expect(r.outs.slice(0, 2)).toEqual(['sent (id: 1)', 'sent (id: 2)'])
  expect(r.outs[2].startsWith('blocked: 不能算用户要求')).toBe(true)   // 额度只在真跨聊天那次记账
  expect(r.sends).toEqual([PRIV, GRP])
}, 20_000)
