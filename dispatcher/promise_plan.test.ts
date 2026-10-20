// promise_plan.ts 单测：时间/gist 校验、工具作用域、欠账整理、文案与提示检测。纯函数。
import { describe, expect, test } from 'bun:test'
import {
  GIST_MAX_CP, OWED_MAX_PER_CHAT, OWED_TTL_MS, PAST_CLAMP_MS, buildPromiseToolRequest,
  detectPromiseHint, gistDedupKey, isPurgeable, localStamp, makeOwedEntry, nextLastHuman,
  parseDueAt, pruneOwed, promiseScopeChat, relativeStamp, renderDueText, renderOwedText,
  resolvePromiseToolChat, sanitizePromiseIds, validateGist, type PromiseRecord,
} from './promise_plan'

const now = 1_700_000_000_000

describe('parseDueAt', () => {
  test('未来数字/数字串/ISO 串', () => {
    expect(parseDueAt(now + 60_000, now)).toEqual({ ok: true, dueAt: now + 60_000, clamped: false })
    expect(parseDueAt(String(now + 120_000), now)).toMatchObject({ ok: true, dueAt: now + 120_000 })
    expect(parseDueAt(new Date(now + 180_000).toISOString(), now)).toMatchObject({ ok: true, dueAt: now + 180_000 })
  })

  test('过去 5 分钟内夹取，超过则拒绝；远未来拒绝', () => {
    const r = parseDueAt(now - 60_000, now)
    expect(r).toMatchObject({ ok: true, clamped: true })
    if (r.ok) expect(r.dueAt).toBe(now + 60_000)
    expect(parseDueAt(now - PAST_CLAMP_MS - 1, now)).toEqual({ ok: false, error: 'due_in_past' })
    expect(parseDueAt(now + 181 * 24 * 3600_000, now)).toEqual({ ok: false, error: 'due_too_far' })
  })

  test('仅日期串/坏类型/坏时钟', () => {
    expect(parseDueAt('2026-10-20', now)).toEqual({ ok: false, error: 'bad_due_at', detail: 'date_only_needs_time' })
    for (const v of ['', 'not a date', null, [], {}, true]) {
      expect(parseDueAt(v, now)).toMatchObject({ ok: false, error: 'bad_due_at' })
    }
    expect(parseDueAt(now + 1000, NaN)).toMatchObject({ ok: false, detail: 'bad_clock' })
  })
})

describe('gist 校验与去重键', () => {
  test('单行化控制字符、去零宽、长度按码点', () => {
    expect(validateGist('a\nb\tc')).toEqual({ ok: true, gist: 'a b c' })
    expect(validateGist('a‮b')).toEqual({ ok: true, gist: 'ab' })
    expect(validateGist('😀'.repeat(GIST_MAX_CP)).ok).toBe(true)
    expect(validateGist('字'.repeat(GIST_MAX_CP + 1))).toEqual({ ok: false, error: 'gist_too_long', len: GIST_MAX_CP + 1 })
    expect(validateGist('   ')).toEqual({ ok: false, error: 'missing_gist' })
    expect(validateGist(1)).toEqual({ ok: false, error: 'missing_gist' })
  })

  test('去重键折叠空白/标点/大小写，纯符号回退原文', () => {
    expect(gistDedupKey('Hello, World!')).toBe(gistDedupKey('hello world'))
    expect(gistDedupKey('「提醒你」')).toBe(gistDedupKey('提醒你'))
    expect(gistDedupKey('💊')).toBe('💊')
    expect(gistDedupKey('⏰')).toBe('⏰')
  })
})

describe('工具作用域', () => {
  const last = { chatId: '-100', userId: 'u1' }
  test('promiseScopeChat 三条同时满足', () => {
    expect(promiseScopeChat('-100', last, '-100')).toBe('-100')
    expect(promiseScopeChat('-100', last, '-200')).toBeNull()
    expect(promiseScopeChat('-200', last, '-100')).toBeNull()
    expect(promiseScopeChat(undefined, last, '-100')).toBeNull()
    expect(promiseScopeChat('-100', null, '-100')).toBeNull()
  })

  test('deliver_to here / requester_private / bad', () => {
    expect(resolvePromiseToolChat({ chat_id: '-100' }, last, ['u1'], '-100'))
      .toEqual({ ok: true, chatId: '-100', sourceChatId: '-100', requesterId: 'u1' })
    expect(resolvePromiseToolChat({ chat_id: '-100', deliver_to: 'requester_private' }, last, ['u1'], '-100'))
      .toEqual({ ok: true, chatId: 'u1', sourceChatId: '-100', requesterId: 'u1' })
    expect(resolvePromiseToolChat({ chat_id: '-100', deliver_to: 'requester_private' }, last, [], '-100'))
      .toEqual({ ok: false, error: 'private_not_allowed' })
    expect(resolvePromiseToolChat({ chat_id: '-100', deliver_to: 'x' }, last, ['u1'], '-100'))
      .toEqual({ ok: false, error: 'bad_deliver_to' })
  })

  test('私聊里 requester_private 等同 here', () => {
    const priv = { chatId: 'u1', userId: 'u1' }
    expect(resolvePromiseToolChat({ chat_id: 'u1', deliver_to: 'requester_private' }, priv, ['u1'], 'u1'))
      .toEqual({ ok: true, chatId: 'u1', sourceChatId: 'u1', requesterId: 'u1' })
  })

  test('buildPromiseToolRequest 组装三种请求', () => {
    expect(buildPromiseToolRequest('promise_list', { chat_id: '-100' }, last, '-100', ['u1']))
      .toEqual({ ok: true, body: { action: 'list', scope_chat_id: '-100' } })
    expect(buildPromiseToolRequest('promise_cancel', { chat_id: '-100', promise_id: 'p1' }, last, '-100', ['u1']))
      .toEqual({ ok: true, body: { action: 'cancel', promise_id: 'p1', scope_chat_id: '-100' } })
    const set = buildPromiseToolRequest('promise_set', { chat_id: '-100', due_at: now + 1000, gist: 'x' }, last, '-100', ['u1'])
    expect(set).toMatchObject({ ok: true, body: { action: 'set', chat_id: '-100', source_chat_id: '-100', requester_id: 'u1' } })
    expect(buildPromiseToolRequest('x', {}, last, '-100', ['u1'])).toEqual({ ok: false, error: 'unknown_tool' })
  })

  test('nextLastHuman 只认首投真人消息', () => {
    const meta = { chat_id: '-100', from_id: 'u1', human_ts: new Date(now).toISOString() }
    expect(nextLastHuman(null, meta, now, false)).toEqual({ chatId: '-100', userId: 'u1' })
    expect(nextLastHuman(null, meta, now, true)).toBeNull()
    expect(nextLastHuman(null, { ...meta, is_bot_sender: true }, now, false)).toBeNull()
    expect(nextLastHuman(null, { chat_id: '-100' }, now, false)).toBeNull()
    expect(nextLastHuman(null, { chat_id: 'u1' }, now, false, ['u1'])).toEqual({ chatId: 'u1', userId: 'u1' })
  })
})

describe('欠账整理', () => {
  const entry = (id: string, due: number, since: number) => ({
    promise_id: id, due_at: due, due_at_local: localStamp(due), gist: id, owed_since: since, reason: 'asleep' as const,
  })

  test('丢坏条目与超 7 天，按 due_at 排序', () => {
    const r = pruneOwed([
      entry('a', 3000, now),
      entry('b', 1000, now),
      entry('old', 0, now - OWED_TTL_MS - 1),
      { nope: true },
    ], now)
    expect(r.kept.map(e => e.promise_id)).toEqual(['b', 'a'])
    expect(r.overflowed).toBe(0)
  })

  test('超过每聊天 10 条丢最旧的 owed_since', () => {
    const list = Array.from({ length: OWED_MAX_PER_CHAT + 2 }, (_, i) => entry(`p${i}`, i, now - 1000 + i))
    const r = pruneOwed(list, now)
    expect(r.kept.length).toBe(OWED_MAX_PER_CHAT)
    expect(r.overflowed).toBe(2)
    expect(r.kept.some(e => e.promise_id === 'p0')).toBe(false)
    expect(r.kept.some(e => e.promise_id === 'p1')).toBe(false)
  })
})

describe('文案与提示', () => {
  test('renderDueText 带前缀、gist 与迟到原因', () => {
    const text = renderDueText('浇花', 60_000, 'asleep', '10/20 08:30')
    expect(text).toContain('[promise-due]')
    expect(text).toContain('浇花')
    expect(text).toContain('你在睡')
    expect(renderDueText('浇花', 7 * 3600_000)).toContain('先承认错误')
  })

  test('renderOwedText 列时刻与原因', () => {
    const text = renderOwedText([{ promise_id: 'p', due_at: now, due_at_local: '10/20 08:30', gist: '浇花', owed_since: now, reason: 'silent' }])
    expect(text).toContain('10/20 08:30')
    expect(text).toContain('浇花')
    expect(text).toContain('没开口')
  })

  test('detectPromiseHint 分句、问句与否定', () => {
    expect(detectPromiseHint('明天找你')).toEqual({ hit: true, phrases: ['明天'] })
    expect(detectPromiseHint('明天找你吗')).toEqual({ hit: false, phrases: [] })
    expect(detectPromiseHint('可能明天找你')).toEqual({ hit: false, phrases: [] })
    expect(detectPromiseHint('今晚给你发消息，明天陪你聊')).toMatchObject({ hit: true })
    expect(detectPromiseHint('')).toEqual({ hit: false, phrases: [] })
  })

  test('relativeStamp / sanitizePromiseIds / isPurgeable', () => {
    expect(relativeStamp(now + 120_000, now)).toBe('约 2 分钟后')
    expect(relativeStamp(now - 120_000, now)).toBe('已过去约 2 分钟')
    expect(sanitizePromiseIds(['a', 'a', 'bad space', 1, 'b'])).toEqual(['a', 'b'])
    expect(sanitizePromiseIds(Array(30).fill(0).map((_, i) => `p${i}`)).length).toBe(20)
    const rec = { state: 'done', closed_at: now - 31 * 24 * 3600_000 } as PromiseRecord
    expect(isPurgeable(rec, now)).toBe(true)
    expect(isPurgeable({ ...rec, closed_at: now }, now)).toBe(false)
  })

  test('makeOwedEntry 保留原定时刻', () => {
    const rec = { chat_id: 'c', due_at: 123, gist: 'g' } as PromiseRecord
    expect(makeOwedEntry('p', rec, 'paused', 999)).toMatchObject({ due_at: 123, owed_since: 999, reason: 'paused' })
  })
})
