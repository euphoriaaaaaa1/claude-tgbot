// promise_runtime.ts 单测：登记幂等、取消、tick 注入与闸、欠账取走/确认。假时钟 + 临时目录 + 注入回调。
import { afterEach, describe, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import {
  OWED_FILE, OWED_INFLIGHT_FILE, STORE_FILE, confirmPromiseOwed, createPromiseRuntime,
  takePromiseOwed, takeoverStaleLock,
} from './promise_runtime'
import { RETRY_AFTER_MS } from './promise_plan'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't19-promise-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

type Ctl = { now: number; probe: 'awake' | 'asleep' | 'unknown' | 'unsupported'; paused: string | null; stopped: boolean; injected: any[]; logs: string[] }

function mkRuntime(over: Partial<Ctl> = {}) {
  const dir = mkDir()
  const ctl: Ctl = { now: 1_700_000_000_000, probe: 'awake', paused: null, stopped: false, injected: [], logs: [], ...over }
  const rt = createPromiseRuntime({
    channelDir: dir,
    inject: (chatId, key, meta) => { ctl.injected.push({ chatId, key, meta }) },
    sessionSize: () => 0,
    scanSession: () => {},
    probeAsleep: async () => ctl.probe,
    isPaused: () => ctl.paused,
    isStopped: () => ctl.stopped,
    now: () => ctl.now,
    log: line => ctl.logs.push(line),
    lockWaitMs: 100,
  })
  return { dir, ctl, rt }
}

const setBody = (chat = 'c1', over: Record<string, unknown> = {}) => ({
  action: 'set', chat_id: chat, gist: '浇花', due_at: 1_700_000_000_000 + 60_000, ...over,
})

describe('登记 / 查询 / 取消', () => {
  test('登记成功并落盘', async () => {
    const { dir, rt } = mkRuntime()
    const r = await rt.handle(setBody())
    expect(r.status).toBe(200)
    expect(r.body.ok).toBe(true)
    expect(String(r.body.promise_id)).toStartWith('p-')
    expect(existsSync(join(dir, STORE_FILE))).toBe(true)
    const store = JSON.parse(readFileSync(join(dir, STORE_FILE), 'utf8'))
    expect(Object.values(store)).toHaveLength(1)
  })

  test('同 chat + 60s 内 + 同 gist 幂等；不同 gist 另记一条', async () => {
    const { rt } = mkRuntime()
    const a = await rt.handle(setBody())
    const b = await rt.handle(setBody('c1', { due_at: (a.body.due_at as number) + 30_000 }))
    expect(b.body.deduped).toBe(true)
    expect(b.body.promise_id).toBe(a.body.promise_id)
    const c = await rt.handle(setBody('c1', { gist: '取快递' }))
    expect(c.body.deduped).toBeUndefined()
    expect(c.body.promise_id).not.toBe(a.body.promise_id)
  })

  test('参数错误与每聊天 20 条上限', async () => {
    const { rt } = mkRuntime()
    expect((await rt.handle({ action: 'nope' })).body.error).toBe('bad_action')
    expect((await rt.handle(setBody('c1', { gist: '' }))).body.error).toBe('missing_gist')
    expect((await rt.handle(setBody('c1', { due_at: 0 }))).body.error).toBe('due_in_past')
    expect((await rt.handle(setBody(''))).body.error).toBe('missing_chat_id')
    for (let i = 0; i < 20; i++) {
      const r = await rt.handle(setBody('c1', { gist: `事${i}`, due_at: 1_700_000_000_000 + 60_000 + i }))
      expect(r.body.ok).toBe(true)
    }
    expect((await rt.handle(setBody('c1', { gist: '第21件', due_at: 1_700_000_100_000 }))).body.error).toBe('too_many_pending')
  })

  test('list 只列 active 且按 scope 过滤；cancel 生效', async () => {
    const { rt } = mkRuntime()
    const a = await rt.handle(setBody('c1'))
    await rt.handle(setBody('c2', { gist: '另一件' }))
    const list = await rt.handle({ action: 'list', scope_chat_id: 'c1' })
    expect(list.body.promises).toHaveLength(1)
    expect(list.body.promises[0].promise_id).toBe(a.body.promise_id)
    expect((await rt.handle({ action: 'list', scope_chat_id: '' })).body.error).toBe('chat_not_allowed')

    const cancel = await rt.handle({ action: 'cancel', promise_id: a.body.promise_id, scope_chat_id: 'c1' })
    expect(cancel.body).toEqual({ ok: true, cancelled: a.body.promise_id })
    expect((await rt.handle({ action: 'cancel', promise_id: a.body.promise_id, scope_chat_id: 'c1' })).body.error).toBe('not_found')
    expect((await rt.handle({ action: 'cancel', promise_id: 'p-x' })).body.error).toBe('not_found')
  })
})

describe('tick', () => {
  test('到期注入一次，fired 后未到重投时间不重复', async () => {
    const { ctl, rt } = mkRuntime()
    const a = await rt.handle(setBody())
    ctl.now += 60_000
    await rt.tick()
    expect(ctl.injected).toHaveLength(1)
    expect(ctl.injected[0].chatId).toBe('c1')
    expect(ctl.injected[0].meta.promise_id).toBe(a.body.promise_id)
    expect(ctl.injected[0].meta.text).toContain('浇花')

    await rt.tick()
    expect(ctl.injected).toHaveLength(1)
    ctl.now += RETRY_AFTER_MS
    await rt.tick()
    // 送达判据始终未命中 → 重投一次（会先撤旧件）
    expect(ctl.injected.length).toBe(2)
  })

  test('暂停闸顺延且不改 due_at', async () => {
    const { ctl, dir, rt } = mkRuntime({ paused: 'bot:c1' })
    await rt.handle(setBody())
    const before = JSON.parse(readFileSync(join(dir, STORE_FILE), 'utf8'))
    const id = Object.keys(before)[0]
    const due = before[id].due_at
    ctl.now += 60_000
    await rt.tick()
    expect(ctl.injected).toHaveLength(0)
    const after = JSON.parse(readFileSync(join(dir, STORE_FILE), 'utf8'))[id]
    expect(after.state).toBe('pending')
    expect(after.due_at).toBe(due)
    expect(after.deferred_reason).toBe('paused')
  })

  test('判睡顺延；醒后经起床缓冲才发', async () => {
    const { ctl, rt } = mkRuntime({ probe: 'asleep' })
    await rt.handle(setBody())
    ctl.now += 60_000
    await rt.tick()
    expect(ctl.injected).toHaveLength(0)
    ctl.probe = 'awake'
    ctl.now += 60_000
    await rt.tick()          // 顺延结束 → 置 defer_until
    expect(ctl.injected).toHaveLength(0)
    ctl.now += 31 * 60_000
    await rt.tick()
    expect(ctl.injected).toHaveLength(1)
  })

  test('功能关停不动已登记数据', async () => {
    const { ctl, rt } = mkRuntime({ stopped: true })
    await rt.handle(setBody())
    ctl.now += 60_000
    await rt.tick()
    expect(ctl.injected).toHaveLength(0)
  })
})

describe('欠账取走 / 确认', () => {
  function seedOwed(dir: string, chat = 'c1') {
    writeFileSync(join(dir, OWED_FILE), JSON.stringify({
      [chat]: [{
        promise_id: 'p1', due_at: 1_700_000_000_000, due_at_local: '11/14 22:13',
        gist: '浇花', owed_since: 1_700_000_000_000, reason: 'asleep',
      }],
    }))
  }

  test('取走：返回认账文本、欠账清空、条目进在途', () => {
    const d = mkDir()
    seedOwed(d)
    const text = takePromiseOwed(d, 'c1', 1_700_000_000_001)
    expect(text).toContain('浇花')
    expect(JSON.parse(readFileSync(join(d, OWED_FILE), 'utf8'))).toEqual({})
    const fl = JSON.parse(readFileSync(join(d, OWED_INFLIGHT_FILE), 'utf8'))
    expect(fl.inflight.c1[0].promise_id).toBe('p1')
  })

  test('确认：清在途并写认账回执；重复确认幂等', () => {
    const d = mkDir()
    seedOwed(d)
    const t = 1_700_000_000_001
    takePromiseOwed(d, 'c1', t)
    expect(confirmPromiseOwed(d, 'c1', t, t + 1000)).toBe(true)
    const fl = JSON.parse(readFileSync(join(d, OWED_INFLIGHT_FILE), 'utf8'))
    expect(fl.inflight.c1).toBeUndefined()
    expect(fl.settled[0].promise_id).toBe('p1')
    expect(confirmPromiseOwed(d, 'c1', t, t + 2000)).toBe(true)
  })

  test('无人欠账时取走返回空串', () => {
    expect(takePromiseOwed(mkDir(), 'c1', 1)).toBe('')
  })

  test('过期锁可被接管，新鲜锁不行', () => {
    const d = mkDir()
    const lock = join(d, 'x.lock')
    writeFileSync(lock, 'old')
    utimesSync(lock, new Date(0), new Date(0))
    expect(takeoverStaleLock(lock, 'new-token')).toBe(true)
    expect(readFileSync(lock, 'utf8')).toBe('new-token')
    expect(takeoverStaleLock(lock, 'another')).toBe(false)
  })
})
