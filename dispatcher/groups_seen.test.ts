// groups_seen.ts 单测：发现合并、限频、50 条上限、迁移、原子写。只用临时目录。
import { afterEach, describe, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import {
  MAX_SEEN_GROUPS, applyGroupMessage, applyMyChatMember, capGroups, emptyDoc,
  minIntervalSecondsFromEnv, normalizeDoc, readGroupsSeenFile, withGroupsSeenLock, writeGroupsSeenFile,
} from './groups_seen'

const T = (iso: string) => Date.parse(iso)
const dirs: string[] = []
function mkFile(): string {
  const d = mkdtempSync(join(tmpdir(), 't19-seen-'))
  dirs.push(d)
  return join(d, 'groups-seen.json')
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

const msg = (chatId: string, title = 'g') => ({ chatId, title, type: 'supergroup' })

describe('normalizeDoc / 读取', () => {
  test('坏形状一律收成空记录', () => {
    expect(normalizeDoc(null).groups).toEqual({})
    expect(normalizeDoc({ groups: [] }).groups).toEqual({})
    expect(normalizeDoc({ groups: { '-1': { title: 3, added_by_owner: 'yes' } } }).groups['-1']).toEqual({
      title: null, type: null, status: null, added_by_owner: null,
      first_seen: null, last_seen: null, migrated_from: null, left_in_console_at: null,
    })
  })

  test('文件不存在 / 坏 JSON 都按空记录并给错误', () => {
    const p = mkFile()
    expect(readGroupsSeenFile(p)).toEqual({ doc: emptyDoc(), error: null })
    writeFileSync(p, '{')
    const r = readGroupsSeenFile(p)
    expect(r.doc.groups).toEqual({})
    expect(r.error).not.toBeNull()
  })

  test('原子写：文件 0600 且可读回', () => {
    const p = mkFile()
    const doc = emptyDoc()
    applyGroupMessage(doc, msg('-100'), 1000, 0, true)
    writeGroupsSeenFile(p, doc)
    expect(statSync(p).mode & 0o777).toBe(0o600)
    expect(JSON.parse(readFileSync(p, 'utf8')).groups['-100'].title).toBe('g')
  })

  test('withGroupsSeenLock 直接执行且拒绝异步回调', () => {
    expect(withGroupsSeenLock('/x', () => 7)).toBe(7)
    expect(() => withGroupsSeenLock('/x', (() => Promise.resolve(1)) as never)).toThrow('必须同步')
  })
})

describe('applyGroupMessage', () => {
  test('陌生群新建；不存在的群且 createIfMissing=false 不写', () => {
    const doc = emptyDoc()
    expect(applyGroupMessage(doc, msg('-1', 'A'), 1000, 0, false)).toBe(false)
    expect(applyGroupMessage(doc, msg('-1', 'A'), 1000, 0, true)).toBe(true)
    expect(doc.groups['-1']).toMatchObject({ title: 'A', added_by_owner: null, status: null })
  })

  test('已有记录只更新 title/last_seen，保留 added_by_owner 与 status', () => {
    const doc = emptyDoc()
    doc.groups['-1'] = {
      title: 'old', type: 'group', status: 'member', added_by_owner: true,
      first_seen: '2026-01-01T00:00:00Z', last_seen: '2026-01-01T00:00:00Z',
      migrated_from: null, left_in_console_at: null,
    }
    expect(applyGroupMessage(doc, msg('-1', 'new'), T('2026-01-02T00:00:00Z'), 0, true)).toBe(true)
    expect(doc.groups['-1']).toMatchObject({
      title: 'new', status: 'member', added_by_owner: true,
      first_seen: '2026-01-01T00:00:00Z', last_seen: '2026-01-02T00:00:00Z',
    })
  })

  test('限频窗口内跳过；0 表示不限频', () => {
    const doc = emptyDoc()
    applyGroupMessage(doc, msg('-1'), 100_000, 60, true)
    expect(applyGroupMessage(doc, msg('-1'), 110_000, 60, true)).toBe(false)
    expect(applyGroupMessage(doc, msg('-1'), 161_000, 60, true)).toBe(true)
    expect(applyGroupMessage(doc, msg('-2'), 161_000, 0, true)).toBe(true)
    expect(applyGroupMessage(doc, msg('-2'), 161_001, 0, true)).toBe(true)
  })
})

describe('capGroups 50 条上限', () => {
  function seeded(count: number, owner: boolean | null): ReturnType<typeof emptyDoc> {
    const doc = emptyDoc()
    for (let i = 1; i <= count; i++) {
      doc.groups[`-${i}`] = {
        title: 'g', type: 'supergroup', status: null, added_by_owner: owner,
        first_seen: new Date(1000 + i).toISOString(), last_seen: new Date(1000 + i).toISOString(),
        migrated_from: null, left_in_console_at: null,
      }
    }
    return doc
  }

  test('优先挤掉非 owner 的最旧记录', () => {
    const doc = seeded(MAX_SEEN_GROUPS + 1, null)
    doc.groups['-1'].added_by_owner = true
    doc.groups['-2'].added_by_owner = true
    capGroups(doc)
    expect(Object.keys(doc.groups).length).toBe(MAX_SEEN_GROUPS)
    expect(doc.groups['-3']).toBeUndefined()   // 最旧的非 owner
    expect(doc.groups['-1']).toBeDefined()
    expect(doc.groups['-2']).toBeDefined()
  })

  test('全是 owner 时只能挤最旧的', () => {
    const doc = seeded(MAX_SEEN_GROUPS + 1, true)
    capGroups(doc)
    expect(Object.keys(doc.groups).length).toBe(MAX_SEEN_GROUPS)
    expect(doc.groups['-1']).toBeUndefined()
  })
})

describe('applyMyChatMember', () => {
  const ev = (over: Record<string, unknown>) => ({
    chatId: '-100', title: 'G', type: 'supergroup', status: 'member',
    actorId: '1', ownerIds: ['1'], ...over,
  })

  test('新群按执行者是否 owner 定 added_by_owner', () => {
    const a = emptyDoc(); applyMyChatMember(a, ev({}), 1000)
    expect(a.groups['-100'].added_by_owner).toBe(true)
    const b = emptyDoc(); applyMyChatMember(b, ev({ actorId: '2' }), 1000)
    expect(b.groups['-100'].added_by_owner).toBe(false)
  })

  test('已有 true/false 不被后续事件改写；null 才定案', () => {
    const doc = emptyDoc()
    applyMyChatMember(doc, ev({}), 1000)
    applyMyChatMember(doc, ev({ actorId: '2', status: 'administrator' }), 2000)
    expect(doc.groups['-100'].added_by_owner).toBe(true)
    expect(doc.groups['-100'].status).toBe('administrator')

    const doc2 = emptyDoc()
    doc2.groups['-100'] = {
      title: null, type: null, status: null, added_by_owner: null,
      first_seen: null, last_seen: null, migrated_from: null, left_in_console_at: 'x',
    }
    applyMyChatMember(doc2, ev({ actorId: '2' }), 3000)
    expect(doc2.groups['-100'].added_by_owner).toBe(false)
    expect(doc2.groups['-100'].left_in_console_at).toBe('x')
  })

  test('超级群迁移：旧记录在时复制到新群号，旧记录保留', () => {
    const doc = emptyDoc()
    doc.groups['-100'] = {
      title: 'old', type: 'group', status: 'member', added_by_owner: true,
      first_seen: 'a', last_seen: 'b', migrated_from: null, left_in_console_at: null,
    }
    applyMyChatMember(doc, ev({ chatId: '-100', migrateTo: '-200' }), 5000)
    expect(doc.groups['-200']).toMatchObject({ title: 'old', migrated_from: '-100', added_by_owner: true })
    expect(doc.groups['-100']).toBeDefined()
  })

  test('超级群迁移：没有旧记录时用新群号建一条', () => {
    const doc = emptyDoc()
    applyMyChatMember(doc, ev({ chatId: '-200', migrateTo: '-100' }), 5000)
    expect(doc.groups['-200']).toMatchObject({
      type: 'supergroup', status: 'member', added_by_owner: null, migrated_from: '-100',
    })
    expect(doc.groups['-100']).toBeUndefined()
  })
})

describe('minIntervalSecondsFromEnv', () => {
  test('缺省 60；0 不限频；负数/非数字按 60', () => {
    expect(minIntervalSecondsFromEnv(undefined)).toBe(60)
    expect(minIntervalSecondsFromEnv('0')).toBe(0)
    expect(minIntervalSecondsFromEnv(' 15 ')).toBe(15)
    for (const v of ['-1', 'x', '1.5', '']) expect(minIntervalSecondsFromEnv(v)).toBe(60)
  })
})
