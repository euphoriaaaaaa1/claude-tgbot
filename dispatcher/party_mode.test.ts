// party_mode.ts 单测：触发/退出解析、状态读写、过期与时钟回拨、参与群判定。只用临时目录。
import { afterEach, describe, expect, test } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import {
  CLOCK_SKEW_MS, PARTY_TTL_MS, deletePartyState, inboundItemSendersAllOwner, isPartyEndPhrase,
  isPartyOptOutPrefix, newPartyState, normalizePartyChatId, normalizePartyExclude, normalizePartyNote,
  ownerOf, ownerWindowOpen, parsePartyMessageIds, parsePartyStateText, participatingPartyGroups,
  partyModeRoot, partyStatePath, readPartyState, refreshPartyState, replyTargetForDelivery, writePartyState,
} from './party_mode'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't19-party-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

describe('输入规范化', () => {
  test('群号只收负整数串', () => {
    expect(normalizePartyChatId('-100123')).toBe('-100123')
    expect(normalizePartyChatId(' -100123 ')).toBe('-100123')
    expect(normalizePartyChatId(-100123)).toBe('-100123')
    for (const v of ['0', '-0', '100', '-01', '-1a', '', null, 1.5]) expect(normalizePartyChatId(v)).toBeNull()
  })

  test('note 最长 80 码点且无换行', () => {
    expect(normalizePartyNote(undefined)).toEqual({ ok: true, note: '' })
    expect(normalizePartyNote('好'.repeat(80)).ok).toBe(true)
    expect(normalizePartyNote('好'.repeat(81)).ok).toBe(false)
    expect(normalizePartyNote('a\nb').ok).toBe(false)
    expect(normalizePartyNote(1).ok).toBe(false)
  })

  test('exclude 按原始长度限制、去重、校验 bot id', () => {
    expect(normalizePartyExclude(['a1', 'a1', 'b2'])).toEqual({ ok: true, exclude: ['a1', 'b2'] })
    expect(normalizePartyExclude(Array(21).fill('a1')).ok).toBe(false)
    expect(normalizePartyExclude(['A']).ok).toBe(false)
    expect(normalizePartyExclude('a1').ok).toBe(false)
  })

  test('owner 取 allowFrom[0]', () => {
    expect(ownerOf({ allowFrom: ['1', '2'] })).toBe('1')
    expect(ownerOf({ allowFrom: [] })).toBe('')
    expect(ownerOf(null)).toBe('')
  })

  test('结束短语与私聊前缀', () => {
    for (const t of ['聚会结束', '（聚会结束）', '我们散了！', ' (结束聚会) ']) expect(isPartyEndPhrase(t)).toBe(true)
    for (const t of ['聚会结束了吗', '散了没', '开始聚会']) expect(isPartyEndPhrase(t)).toBe(false)
    expect(isPartyOptOutPrefix('私聊：今晚有事')).toBe(true)
    expect(isPartyOptOutPrefix('私聊')).toBe(false)
  })
})

describe('状态文件', () => {
  const now = 1_700_000_000_000
  const state = newPartyState('-100', 'console', '吃火锅', now, ['bot2'])

  test('解析合法状态', () => {
    const r = parsePartyStateText(JSON.stringify(state), now + 1000)
    expect(r.status).toBe('ok')
    expect(r.state).toMatchObject({ on: true, source: 'console', note: '吃火锅', exclude: ['bot2'] })
  })

  test('坏 JSON / 版本 / 形状 → unreadable', () => {
    for (const text of ['{', '[]', JSON.stringify({ ...state, v: 2 }), JSON.stringify({ ...state, on: false }),
      JSON.stringify({ ...state, chat_id: '100' }), JSON.stringify({ ...state, source: 'x' })]) {
      expect(parsePartyStateText(text, now).status).toBe('unreadable')
    }
  })

  test('时钟回拨与满 6 小时过期', () => {
    expect(parsePartyStateText(JSON.stringify(state), now - CLOCK_SKEW_MS - 1).status).toBe('clock_skew')
    expect(parsePartyStateText(JSON.stringify(state), now + PARTY_TTL_MS).status).toBe('expired')
    expect(parsePartyStateText(JSON.stringify(state), now + PARTY_TTL_MS - 1).status).toBe('ok')
  })

  test('读不到 vs 不存在', () => {
    const root = mkDir()
    expect(readPartyState(partyStatePath(root, '-100'), now).status).toBe('missing')
    const p = partyStatePath(root, '-100')
    writeFileSync(p, 'x'.repeat(5000))
    expect(readPartyState(p, now).status).toBe('unreadable')
  })

  test('写读删往返；删两次幂等', () => {
    const root = mkDir()
    const p = partyStatePath(root, '-100')
    expect(writePartyState(p, state)).toBe(true)
    expect(readPartyState(p, now + 1000).state?.note).toBe('吃火锅')
    expect(deletePartyState(p)).toBe(true)
    expect(deletePartyState(p)).toBe(true)
    expect(readPartyState(p, now).status).toBe('missing')
  })

  test('重复开启保留首次 opened_at/source，显式变更才更新', () => {
    const again = refreshPartyState(state, { note: '改吃烧烤' })
    expect(again.opened_at_ms).toBe(state.opened_at_ms)
    expect(again.source).toBe('console')
    expect(again.note).toBe('改吃烧烤')
    expect(again.exclude).toEqual(['bot2'])
    const keep = refreshPartyState(state, {})
    expect(keep.note).toBe('吃火锅')
  })

  test('非法群号拼路径直接拒绝', () => {
    expect(() => partyStatePath('/x', '../etc')).toThrow()
  })

  test('partyModeRoot 只认绝对路径覆盖', () => {
    expect(partyModeRoot({ DISPATCHER_PARTY_DIR: '/tmp/x' })).toBe('/tmp/x')
    expect(partyModeRoot({ DISPATCHER_PARTY_DIR: 'relative' }).endsWith('.party-mode')).toBe(true)
  })
})

describe('参与与权限', () => {
  const now = 1_700_000_000_000
  test('participatingPartyGroups 过滤 exclude 与坏状态', () => {
    const root = mkDir()
    writePartyState(partyStatePath(root, '-100'), newPartyState('-100', 'bot', '', now, ['bota']))
    writePartyState(partyStatePath(root, '-200'), newPartyState('-200', 'bot', '', now))
    const r = participatingPartyGroups({
      partyRoot: root, accessGroups: { '-100': {}, '-200': {}, '-300': {}, bad: {} },
      botName: 'bota', owner: '1', groupsFeatureOn: true, nowMs: now + 1,
    })
    expect(r.groups.map(g => g.chat_id)).toEqual(['-200'])
    expect(r.invalidNames).toEqual(['bad'])
    expect(r.invalid).toEqual([])
  })

  test('groups 功能关或 owner 缺失 → 不读文件直接空', () => {
    const r = participatingPartyGroups({
      partyRoot: '/no/such', accessGroups: { '-1': {} }, botName: 'b', owner: '', groupsFeatureOn: true, nowMs: now,
    })
    expect(r.groups).toEqual([])
    const r2 = participatingPartyGroups({
      partyRoot: '/no/such', accessGroups: { '-1': {} }, botName: 'b', owner: '1', groupsFeatureOn: false, nowMs: now,
    })
    expect(r2.groups).toEqual([])
  })

  test('owner 发言窗口边界', () => {
    expect(ownerWindowOpen(1000, 1000, 0)).toBe(true)
    expect(ownerWindowOpen(1001, 1000, 0)).toBe(false)
    expect(ownerWindowOpen(1000, 1001, 100)).toBe(false)
    expect(ownerWindowOpen(2000, 1000, 999)).toBe(false)
    expect(ownerWindowOpen(2000, 1000, 1000)).toBe(true)
    expect(ownerWindowOpen(2000, 'x', 1000)).toBe(false)
  })

  test('message_id 列表与全部 owner 判定', () => {
    expect(parsePartyMessageIds(12)).toEqual(['12'])
    expect(parsePartyMessageIds('1,2,3')).toEqual(['1', '2', '3'])
    for (const v of ['', '0', '1,,2', 'a', Array(201).fill('1').join(','), null]) expect(parsePartyMessageIds(v)).toBeNull()
    const ledger = new Map([
      ['-1|1', { senderId: 'o', atMs: 1 }],
      ['-1|2', { senderId: 'x', atMs: 1 }],
    ])
    expect(inboundItemSendersAllOwner(ledger, '-1', '1', 'o')).toBe(true)
    expect(inboundItemSendersAllOwner(ledger, '-1', '1,2', 'o')).toBe(false)
    expect(inboundItemSendersAllOwner(ledger, '-1', '9', 'o')).toBe(false)
  })

  test('reply 默认目标：显式优先，其次 party_chat_id（非 ambiguous）', () => {
    expect(replyTargetForDelivery('x', { chat_id: 'c', party_chat_id: 'p' })).toBe('x')
    expect(replyTargetForDelivery(null, { chat_id: 'c', party_chat_id: 'p' })).toBe('p')
    expect(replyTargetForDelivery(null, { chat_id: 'c', party_chat_id: 'p', party_ambiguous: true })).toBe('c')
    expect(replyTargetForDelivery(undefined, null)).toBe('')
  })
})
