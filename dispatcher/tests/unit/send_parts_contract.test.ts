// POST /send 的 parts 回执契约（INTERFACE.md 表格 POST /send 一行）。
// 黑盒：起真实 dispatcher HTTP 服务，但 Telegram 全部走本机假服务，绝不外发。
import { test, expect, beforeAll, afterAll, beforeEach } from 'bun:test'
import {
  startDispatcherServer, tgReset, tgSetHandler, tgDefaultHandler, cleanupBase,
  type DispatcherServer,
} from '../../../tests/acceptance/h3b_delivery/_fixtures.ts'

let srv: DispatcherServer
beforeAll(async () => { srv = await startDispatcherServer() })
afterAll(() => { try { srv?.stop() } catch { /* ignore */ } cleanupBase() })
beforeEach(() => { tgReset(); tgDefaultHandler() })

const H = () => ({ 'content-type': 'application/json', 'x-api-token': srv.token })
async function post(body: unknown): Promise<{ status: number; text: string; json: any }> {
  const r = await fetch(`${srv.base}/send`, { method: 'POST', headers: H(), body: JSON.stringify(body) })
  const text = await r.text()
  let json: any = null
  try { json = JSON.parse(text) } catch { /* 非 JSON */ }
  return { status: r.status, text, json }
}

test('成功响应的 parts 逐段覆盖且顺序与 message_ids 一致', async () => {
  const r = await post({ chat_id: '123456789', text: '契约回执' })
  expect(r.status).toBe(200)
  const parts = r.json?.parts
  const ids = r.json?.message_ids
  expect(Array.isArray(parts)).toBe(true)
  expect(Array.isArray(ids)).toBe(true)
  expect(parts.length).toBe(ids.length)
  parts.forEach((p: any, i: number) => {
    expect(p.index).toBe(i)
    expect(p.ok).toBe(true)
    expect(p.message_id).toBe(ids[i])
    expect(p.kind).toBe('text')
    expect(p.reason).toBeNull()
  })
  // 不回显正文
  expect(r.text).not.toContain('契约回执')
})

test('失败段 ok:false、message_id:null 且带既有原因码（全失败 502）', async () => {
  tgSetHandler((method) => {
    if (method === 'sendMessage') {
      return new Response(JSON.stringify({ ok: false, error_code: 400, description: 'Bad Request' }),
        { status: 400, headers: { 'content-type': 'application/json' } })
    }
    return new Response(JSON.stringify({ ok: true, result: { message_id: 1, chat: { id: 1 }, date: 0 } }),
      { headers: { 'content-type': 'application/json' } })
  })
  const r = await post({ chat_id: '123456789', text: '会失败' })
  expect(r.status).toBe(502)
  expect(r.json.parts.length).toBe(1)
  const p = r.json.parts[0]
  expect(p.index).toBe(0)
  expect(p.ok).toBe(false)
  expect(p.message_id).toBeNull()
  expect(p.kind).toBe('text')
  expect(p.reason).toBe('telegram_4xx')
  expect(r.json.message_ids).toEqual([])
})

test('400 bad_request 错误体含 parts:[]', async () => {
  const r = await post({ text: '缺 chat_id' })
  expect(r.status).toBe(400)
  expect(r.json.error).toBe('bad_request')
  expect(r.json.parts).toEqual([])
})
