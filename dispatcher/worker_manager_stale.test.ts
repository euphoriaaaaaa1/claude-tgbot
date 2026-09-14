// 白盒：worker-manager pump() 的 ⑤ 陈旧合成件丢弃 + ③b 真人统一计时（不起 claude，dispatchContent 用桩接住）
import { test, expect } from 'bun:test'
import { ROOT } from './tests/acceptance/desync/_env.ts'
import { existsSync, mkdirSync, utimesSync, writeFileSync } from 'fs'
import { join } from 'path'
import { buildTimePrefix } from './time_annotate'

// 模块级 CHANNEL_DIR 在首次 import 时定死：先指到测试根下再动态 import（别的测试文件先导入也只会是临时目录）
process.env.CHANNEL_DIR ??= join(ROOT, 'channels', 'wm')
const { WorkerManager } = await import('./worker-manager')
const DIR = join(ROOT, 'wm-inbox')
mkdirSync(DIR, { recursive: true })

function mgr() {
  const m = Object.create(WorkerManager.prototype) as any
  m.inFlight = null; m.phase = 'ready'; m.queue = []
  m.sent = [] as string[]
  m.dispatchContent = (_i: unknown, content: string) => { m.sent.push(content) }
  m.writeLastChatId = () => {}
  return m
}
function put(name: string, meta: Record<string, unknown>, ageMs = 0): string {
  const p = join(DIR, name)
  writeFileSync(p, JSON.stringify(meta))
  const t = (Date.now() - ageMs) / 1000
  utimesSync(p, t, t)
  return p
}

test('⑤ 陈旧合成件丢弃且不进合并；新鲜合成件与再旧的真人件照投', () => {
  const m = mgr()
  const stale = put('director-1.json', { chat_id: '-100', scene: 'group', text: 'STALE' }, 31 * 60_000)
  const fresh = put('self-2.json', { chat_id: '-100', scene: 'group', text: 'FRESH' })
  const human = put('-100_3.json', { chat_id: '-100', scene: 'group', text: 'HUMAN' }, 3 * 86400_000)
  m.queue = [stale, fresh, human].map(path => ({ kind: 'file', path }))
  m.pump()
  expect(m.sent.length).toBe(1)
  expect(m.sent[0]).toContain('FRESH')
  expect(m.sent[0]).toContain('HUMAN')
  expect(m.sent[0]).not.toContain('STALE')
  expect(existsSync(stale)).toBe(false)
  expect(m.queue.length).toBe(0)
})

test('③b 间隔按最近真人消息统一计时：别的群里的合成消息不重置、也不用本群旧值', () => {
  const m = mgr()
  const h1 = Math.floor((Date.now() - 8 * 3600_000) / 1000) * 1000
  const iso = (ms: number) => new Date(ms).toISOString()
  const run = (name: string, meta: Record<string, unknown>) => {
    m.queue = [{ kind: 'file', path: put(name, meta) }]
    m.pump()
    return m.sent[m.sent.length - 1] as string
  }
  run('-100_1.json', { chat_id: '-100', scene: 'group', text: 'hi', ts: iso(h1), human_ts: iso(h1) })
  const t2 = iso(Date.now())
  expect(run('director-2.json', { chat_id: '-200', scene: 'group', from_username: 'director', text: '[director] x', ts: t2 }))
    .toContain(buildTimePrefix(Date.parse(t2), h1))
  const t3 = iso(Date.now())
  expect(run('self-3.json', { chat_id: '-200', scene: 'group', text: '[self-initiate] y', ts: t3 }))
    .toContain(buildTimePrefix(Date.parse(t3), h1))
})
