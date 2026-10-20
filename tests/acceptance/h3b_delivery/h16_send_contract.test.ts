// H3b · H-16 发送丢段可见性 + H-17 中途失败不发两遍/耗时封顶 —— 黑盒验收
// 只依据 .devflow/INTERFACE-h3b-delivery.md 四(10-17)。不读实现。
// 全程假 Telegram（api.telegram.org 被改写到本机假服务），绝不发真请求。
// bug 修复模式：契约要求的 planned/sent/dropped/idempotent 与 HTTP 分级当前均缺失/不符 → 应为红。
import { test, expect, beforeAll, afterAll, beforeEach } from 'bun:test'
import {
  startDispatcherServer, tgReset, tgSetHandler, tgDefaultHandler, tgCount, tgCallsOf, makeImage, snapshotDir, CHANNEL_DIR,
  cleanupBase, BOT_TOKEN, type DispatcherServer,
} from './_fixtures.ts'

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

const REASONS = new Set(['network', 'telegram_4xx', 'telegram_5xx', 'timeout'])
const okMsg = { ok: true, result: { message_id: 1, chat: { id: 1 }, date: 0 } }
const errMsg = (code: number, desc: string) => new Response(
  JSON.stringify({ ok: false, error_code: code, description: desc }),
  { status: code, headers: { 'content-type': 'application/json' } })

// ─── 契约 11：/send 响应体恒含 planned / sent / dropped ────────────────────
test('响应体恒含 planned_sent_dropped（全送达）', async () => {
  const r = await post({ chat_id: '123456789', text: '你好' })
  expect(r.status).toBeGreaterThanOrEqual(200); expect(r.status).toBeLessThan(300)
  expect(typeof r.json?.planned).toBe('number')
  expect(typeof r.json?.sent).toBe('number')
  expect(Array.isArray(r.json?.dropped)).toBe(true)
  expect(r.json.sent).toBe(r.json.planned)
  expect(r.json.dropped).toEqual([])
})

// ─── 契约 11b：未带 batch_id → 不幂等，响应 idempotent:false ───────────────
test('无 batch_id_响应 idempotent:false', async () => {
  const r = await post({ chat_id: '123456789', text: 'hi' })
  expect(r.json?.idempotent).toBe(false)
})

// ─── 契约 10：引用失败 → 去引用重发成功则计入 sent、不进 dropped ───────────
test('引用失败去引用重发成功_计入sent且dropped为空', async () => {
  let calls = 0
  tgSetHandler((method, body) => {
    if (method === 'getUpdates') return new Response(JSON.stringify({ ok: true, result: [] }), { headers: { 'content-type': 'application/json' } })
    if (method === 'getMe') return new Response(JSON.stringify({ ok: true, result: { id: 1, is_bot: true, username: 'x' } }), { headers: { 'content-type': 'application/json' } })
    if (method === 'sendMessage') {
      calls++
      if (body?.reply_parameters) return errMsg(400, 'Bad Request: message to be replied not found')
      return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
    }
    return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
  })
  const r = await post({ chat_id: '123456789', text: '带引用的回复', reply_to: 7 })
  expect(r.status).toBeGreaterThanOrEqual(200); expect(r.status).toBeLessThan(300)
  expect(r.json?.dropped).toEqual([])
  expect(r.json?.sent).toBe(r.json?.planned)
  expect(r.json?.sent).toBeGreaterThan(0)
  expect(calls).toBeGreaterThanOrEqual(2) // 原始 1 次 + 去引用 1 次
})

// ─── 契约 12：部分丢段 → 2xx + dropped 如实 ────────────────────────────────
test('部分丢段_2xx且dropped如实列出', async () => {
  const img = makeImage('p1.png')
  const firstOk = { done: false }
  tgSetHandler((method, body) => {
    if (method === 'sendPhoto') {
      if (!firstOk.done) { firstOk.done = true; return errMsg(400, 'Bad Request: wrong file') }
      return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
    }
    return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
  })
  const r = await post({ chat_id: '123456789', text: '看图', files: [img] })
  expect(r.status).toBeGreaterThanOrEqual(200); expect(r.status).toBeLessThan(300) // sent(text)>0
  expect(Array.isArray(r.json?.dropped)).toBe(true)
  expect(r.json.dropped.length).toBe(1)
  expect(r.json.planned).toBe(2)   // 文本 + 1 图
  expect(r.json.sent).toBe(1)
})

// ─── 契约 11：dropped[].index 从 0 起、顺序与计划一致；reason 取值受限 ──────
test('dropped的index从0起_与计划顺序一致_reason在固定集合内', async () => {
  const a = makeImage('a.png'), b = makeImage('b.png')
  let photo = 0
  tgSetHandler((method, _body) => {
    if (method === 'sendPhoto') {
      photo++
      if (photo === 2) return errMsg(500, 'Internal Server Error') // 计划里第 2 张图（index 2）失败
      return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
    }
    return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
  })
  const r = await post({ chat_id: '123456789', text: 't', files: [a, b] })
  expect(r.json.planned).toBe(3) // text, a, b
  const dropped = r.json.dropped as any[]
  expect(Array.isArray(dropped)).toBe(true)
  expect(dropped.map(d => d.index)).toEqual([2])       // 顺序一致、0 起
  for (const d of dropped) expect(REASONS.has(d.reason)).toBe(true)
  expect(dropped[0].reason).toBe('telegram_5xx')
})

// ─── 契约 12：全丢 → 502 + 同样三字段 ─────────────────────────────────────
test('全丢_502且带planned_sent_dropped', async () => {
  tgSetHandler((method) => {
    if (method === 'sendMessage') return errMsg(400, 'Bad Request: chat not found')
    return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
  })
  const r = await post({ chat_id: '123456789', text: '只有文本' })
  expect(r.status).toBe(502)
  expect(r.json?.planned).toBe(1)
  expect(r.json?.sent).toBe(0)
  expect(Array.isArray(r.json?.dropped)).toBe(true)
  expect(r.json.dropped.length).toBe(1)
  expect(r.json.dropped[0].index).toBe(0)
  expect(REASONS.has(r.json.dropped[0].reason)).toBe(true)
})

// ─── 契约 14/15：文件段失败按段跳过，不整条无信息 500；剩余段仍按序发 ──────
test('文件段失败_按段跳过_不整条500_后续段照发', async () => {
  const a = makeImage('a2.png'), b = makeImage('b2.png')
  let photo = 0
  tgSetHandler((method) => {
    if (method === 'sendPhoto') { photo++; if (photo === 1) return errMsg(400, 'Bad Request: wrong file') }
    return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
  })
  const r = await post({ chat_id: '123456789', text: 't', files: [a, b] })
  expect(r.status).toBeGreaterThanOrEqual(200); expect(r.status).toBeLessThan(300)
  expect(r.status).not.toBe(500)
  expect(r.json.sent).toBe(2)             // text + 第二张图
  expect(r.json.dropped.map((d: any) => d.index)).toEqual([1])
  // 剩余段（第二张图）确实发过：sendPhoto 调用 2 次（失败 1 + 成功 1）
  expect(tgCount('sendPhoto')).toBe(2)
})

// ─── 契约 17：同一段最多发 2 次（原始 + 去引用重发），跳段不打乱后续顺序 ────
test('引用一直失败_同段最多重试2次_不无限重试', async () => {
  tgSetHandler((method) => {
    if (method === 'sendMessage') return errMsg(400, 'Bad Request: message to be replied not found')
    return new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } })
  })
  const r = await post({ chat_id: '123456789', text: '永远失败', reply_to: 9 })
  expect(tgCount('sendMessage')).toBeLessThanOrEqual(2) // 每段最多 2 次
  expect(tgCount('sendMessage')).toBeGreaterThanOrEqual(2) // 应有去引用重发
  expect(r.status).toBe(502)                              // 全丢
  expect(r.json.dropped.length).toBe(1)
})

// ─── 契约 11b：同一 batch_id 重复请求 → 第二次零发送 + 响应逐字相同 ─────────
test('同batch_id重复请求_第二次零发送_响应逐字相同', async () => {
  const body = { chat_id: '123456789', text: '幂等测试', batch_id: 'BATCH-B1' }
  const first = await post(body)
  const n1 = tgCount('sendMessage')
  expect(n1).toBeGreaterThan(0)
  const second = await post(body)
  expect(tgCount('sendMessage')).toBe(n1)          // 第二次不再发送任何段
  expect(second.text).toBe(first.text)             // 响应与首次逐字相同
})

// ─── 反向：不同 batch_id 各自发送（证明幂等不是"一律回放"）─────────────────
test('不同batch_id_各自照常发送', async () => {
  await post({ chat_id: '123456789', text: 'x1', batch_id: 'BATCH-C1' })
  const n1 = tgCount('sendMessage')
  await post({ chat_id: '123456789', text: 'x2', batch_id: 'BATCH-C2' })
  expect(tgCount('sendMessage')).toBeGreaterThan(n1)
})

// ─── 反向：非 2xx 也不得写坏通道目录（只读发送，零落盘副作用）──────────────
test('全丢502_通道目录零改动', async () => {
  const before = snapshotDir(CHANNEL_DIR)
  tgSetHandler((method) => method === 'sendMessage'
    ? errMsg(500, 'boom')
    : new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } }))
  await post({ chat_id: '123456789', text: 'x' })
  expect(snapshotDir(CHANNEL_DIR)).toEqual(before)
})

// 契约 9b：响应里的"未送达原因"不得包含用户正文
test('dropped原因不含用户正文', async () => {
  const secret = 'SECRET-USER-BODY-123'
  tgSetHandler((method) => method === 'sendMessage'
    ? errMsg(400, `Bad Request: ${secret}`)
    : new Response(JSON.stringify(okMsg), { headers: { 'content-type': 'application/json' } }))
  const r = await post({ chat_id: '123456789', text: secret })
  expect(r.text.includes(secret)).toBe(false)       // 正文不得回显到响应
})
