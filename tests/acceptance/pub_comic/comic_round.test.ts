// pub-comic · POST /comic_round 对外契约（行为清单 2 后半, 10 后半, 13；接口文件 B5）
// 真 dispatcher 子进程 + 本机假 Telegram，只走 HTTP。
import { test, expect, afterEach } from 'bun:test'
import { writeFileSync } from 'node:fs'
import { API_TOKEN, CHATS, comicHarness, stopAll } from './_comic_harness.ts'
import type { Comic } from './_comic_harness.ts'

afterEach(async () => { await stopAll() })
const DAY = 24 * 3600 * 1000
const INACTIVE = { ok: true, active: false, round: 0, expires_at_ms: 0 }

async function raw(c: Comic, method: string, path: string, body?: string, auth = true): Promise<{ status: number; json: any }> {
  const r = await fetch(`http://127.0.0.1:${c.h.port}${path}`, {
    method,
    headers: { 'content-type': 'application/json', ...(auth ? { authorization: `Bearer ${API_TOKEN}`, 'x-api-token': API_TOKEN } : {}) },
    body,
    signal: AbortSignal.timeout(30000),
  })
  const text = await r.text()
  let json: any = null
  try { json = JSON.parse(text) } catch { /* 非 JSON */ }
  return { status: r.status, json }
}
const okBody = (extra: Record<string, unknown> = {}) => JSON.stringify({ chat_id: CHATS[0], delivery_key: 'k1', ...extra })

// ─── 行为 13 ───────────────────────────────────────────────────
test('不带口令头_拒绝码与photo_round完全相同且不是200', async () => {
  const c = await comicHarness()
  const photo = await raw(c, 'POST', '/photo_round', okBody(), false)
  const comic = await raw(c, 'POST', '/comic_round', okBody(), false)
  expect(photo.status).toBeGreaterThanOrEqual(400)
  expect(comic.status).toBe(photo.status)
}, 90000)

test('坏JSON_400_detail是body', async () => {
  const c = await comicHarness()
  const r = await raw(c, 'POST', '/comic_round', '{不是json')
  expect(r.status).toBe(400)
  expect(r.json).toEqual({ ok: false, error: 'bad_request', detail: 'body' })
}, 90000)

test('chat_id非法_400_detail是chat_id', async () => {
  const c = await comicHarness()
  for (const bad of ['../evil', 'abc', '1e3', '']) {
    const r = await raw(c, 'POST', '/comic_round', okBody({ chat_id: bad }))
    expect([bad, r.status, r.json]).toEqual([bad, 400, { ok: false, error: 'bad_request', detail: 'chat_id' }])
  }
}, 90000)

test('delivery_key为空或超512字_400_detail是delivery_key', async () => {
  const c = await comicHarness()
  for (const bad of ['', 'k'.repeat(513)]) {
    const r = await raw(c, 'POST', '/comic_round', okBody({ delivery_key: bad }))
    expect([bad.length, r.status, r.json]).toEqual([bad.length, 400, { ok: false, error: 'bad_request', detail: 'delivery_key' }])
  }
}, 90000)

test('delivery_key恰好512字_合法_返回200', async () => {
  const c = await comicHarness()
  const r = await raw(c, 'POST', '/comic_round', okBody({ delivery_key: 'k'.repeat(512) }))
  expect(r.status).toBe(200)
  expect(r.json.ok).toBe(true)
}, 90000)

test('GET_comic_round_返回405', async () => {
  const c = await comicHarness()
  const r = await raw(c, 'GET', '/comic_round')
  expect(r.status).toBe(405)
}, 90000)

// ─── B5：无状态 / image 关 / 过期 ───────────────────────────────────
test('没有漫画状态_200且active为false_round与过期时刻为0', async () => {
  const c = await comicHarness()
  const r = await raw(c, 'POST', '/comic_round', okBody())
  expect(r.status).toBe(200)
  expect(r.json).toEqual(INACTIVE)
}, 90000)

test('image关闭_comic_round返回active为false的固定形状_且不建状态', async () => {
  const c = await comicHarness({ imageOn: false })
  const r = await raw(c, 'POST', '/comic_round', okBody())
  expect(r.status).toBe(200)
  expect(r.json).toEqual(INACTIVE)
  expect(c.filesUnder(c.comicRoot)).toEqual([])
}, 90000)

// ─── 行为 2 后半 ─────────────────────────────────────────────────
test('同一delivery_key连调两次_返回同一个round_换key才加一轮', async () => {
  const c = await comicHarness()
  c.say('（漫画）')
  await c.stateAppears(c.comicPath(CHATS[0]))
  const a = await raw(c, 'POST', '/comic_round', okBody({ delivery_key: 'same' }))
  const b = await raw(c, 'POST', '/comic_round', okBody({ delivery_key: 'same' }))
  expect(a.status).toBe(200)
  expect(a.json.ok).toBe(true)
  expect(a.json.active).toBe(true)
  expect(a.json.round).toBe(1)
  expect(typeof a.json.expires_at_ms).toBe('number')
  expect(b.json).toEqual(a.json)
  const n = await raw(c, 'POST', '/comic_round', okBody({ delivery_key: 'other' }))
  expect(n.json.round).toBe(2)
  expect(c.readJson(c.comicPath(CHATS[0])).rounds).toBe(2)
}, 120000)

test('状态已满24小时_comic_round返回active为false_文件被删_日志comic_state_expired', async () => {
  const c = await comicHarness()
  const chat = CHATS[0]
  c.say('（漫画）')
  const st = await c.stateAppears(c.comicPath(chat))
  writeFileSync(c.comicPath(chat), JSON.stringify({ ...st, opened_at_ms: Date.now() - DAY - 5000 }))
  const r = await raw(c, 'POST', '/comic_round', okBody())
  expect(r.status).toBe(200)
  expect(r.json).toEqual(INACTIVE)
  await c.eventually(() => !c.has(c.comicPath(chat)), '状态文件被删', 10000)
  expect(c.h.stderr()).toContain(`comic_state_expired kind=comic chat=${chat}`)
}, 120000)
