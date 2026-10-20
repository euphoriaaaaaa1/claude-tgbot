// H3b · 契约 11b 幂等台账边界 —— 黑盒验收
// 只依据 .devflow/INTERFACE-h3b-delivery.md 四(11b)：同一 batch_id 重复请求直接返回首次结果、不再发送；
// 台账有界（内存 + TTL 10 分钟，上限 100 条）。
// 全程假 Telegram，绝不发真请求。
// 契约未固定台账时钟注入方式，本夹具按 { now } 工厂选项注入（假设 A5，写在 TEST-PLAN）。
import { test, expect, beforeAll, afterAll } from 'bun:test'
import { startDispatcherServer, tgReset, tgDefaultHandler, tgCount, cleanupBase, type DispatcherServer } from './_fixtures.ts'

let srv: DispatcherServer
beforeAll(async () => { srv = await startDispatcherServer() })
afterAll(() => { try { srv?.stop() } catch { /* ignore */ } cleanupBase() })

async function post(body: unknown) {
  const r = await fetch(`${srv.base}/send`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-token': srv.token }, body: JSON.stringify(body) })
  const text = await r.text()
  return { status: r.status, text }
}

test('同batch_id第三次请求_仍回放首次结果_零新发送', async () => {
  tgReset(); tgDefaultHandler()
  const body = { chat_id: '123456789', text: '三次幂等', batch_id: 'IDEM-THIRD' }
  const a = await post(body)
  const n1 = tgCount('sendMessage')
  const b = await post(body)
  const c = await post(body)
  expect(tgCount('sendMessage')).toBe(n1)   // 第二、三次都不再发送
  expect(b.text).toBe(a.text)
  expect(c.text).toBe(a.text)               // 与首次逐字相同
})

test('台账上限100_第101条挤出最早一条_被挤出的重发会再发', async () => {
  tgReset(); tgDefaultHandler()
  const first = { chat_id: '123456789', text: 'first', batch_id: 'IDEM-LIMIT-001' }
  await post(first) // 记入台账（最早）
  for (let i = 2; i <= 101; i++) await post({ chat_id: '123456789', text: 'm' + i, batch_id: 'IDEM-LIMIT-' + String(i).padStart(3, '0') })
  const nBefore = tgCount('sendMessage')
  // 最近一条（第 101 条）仍在台账 → 不应再发
  await post({ chat_id: '123456789', text: 'm101', batch_id: 'IDEM-LIMIT-101' })
  expect(tgCount('sendMessage')).toBe(nBefore)          // 命中台账，零发送
  // 最早一条已被上限挤出 → 重发应真正发送
  await post(first)
  expect(tgCount('sendMessage')).toBe(nBefore + 1)      // 已过期/被挤出 → 重新发送
}, 30000)

test('TTL十分钟_过期后重发会再发（注入时钟驱动）', async () => {
  let clock = 1_000_000_000
  const s2 = await startDispatcherServer({ now: () => clock })
  try {
    tgReset(); tgDefaultHandler()
    const body = { chat_id: '123456789', text: 'ttl', batch_id: 'IDEM-TTL-1' }
    const send = async () => {
      const r = await fetch(`${s2.base}/send`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-token': s2.token }, body: JSON.stringify(body) })
      return r.text()
    }
    const t1 = await send()
    const n1 = tgCount('sendMessage')
    await send()
    expect(tgCount('sendMessage')).toBe(n1)          // 未过期：命中台账，零发送
    clock += 10 * 60 * 1000 + 1000                   // 快进 10 分钟 + 1 秒
    const t3 = await send()
    expect(tgCount('sendMessage')).toBe(n1 + 1)      // 已过期：重新发送
    expect(t3).not.toBe(t1)
  } finally { try { s2.stop() } catch { /* ignore */ } }
}, 20000)
