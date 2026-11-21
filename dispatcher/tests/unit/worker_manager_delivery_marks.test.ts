// T18 worker-manager 投递标记：party/拍照标记必须落在场景标注之前（clear_summary 按场景标注切注入），
// 且用户正文里的同名方括号已由 buildContent 转全角。白盒，不起 claude。
import { test, expect, afterAll } from 'bun:test'
import { ROOT } from '../acceptance/desync/_env.ts'
import { join } from 'path'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'

process.env.CHANNEL_DIR ??= join(ROOT, 'channels', 'wm-marks')
// /photo_round、/comic_round 地址在导入时定下：指向本机死端口，"连不上"分支不会碰到真实 dispatcher
process.env.DISPATCHER_PORT = '9'
const { WorkerManager } = await import('../../worker-manager')
const { nextPhotoRound, triggerPhotoState } = await import('../../photo_mode')

function mgr() {
  const m = Object.create(WorkerManager.prototype) as any
  m.buildContent = (meta: any) => `【私聊】<channel chat_id="${meta.chat_id}">\n${meta.text}\n</channel>`
  return m
}

test('party 标记插在场景标注之前', () => {
  const m = mgr()
  const out = m.buildDeliveryContent([{ chat_id: '123456789', text: 'hi', party_chat_id: '-1001234567890' }])
  expect(out).toContain('[party] 线下聚会进行中（群 -1001234567890）')
  expect(out.indexOf('[party]')).toBeLessThan(out.indexOf('【私聊】'))
})

test('多个群聚会 → party-ambiguous 标记', () => {
  const m = mgr()
  const out = m.buildDeliveryContent([{ chat_id: '123456789', text: 'hi', party_ambiguous: true }])
  expect(out).toContain('[party-ambiguous]')
  expect(out).not.toContain('[party] 线下')
})

test('拍照标记同样插在场景标注之前', () => {
  const m = mgr()
  const base = m.buildDeliveryContent([{ chat_id: '123456789', text: 'hi' }])
  const out = m.injectDeliveryMarks(base, ['[auto-photo] 第2轮：x'])
  expect(out.indexOf('[auto-photo]')).toBeLessThan(out.indexOf('【私聊】'))
})

test('photoRequestFor：仅 active 且非 off 且有 delivery_key', () => {
  const m = mgr()
  expect(m.photoRequestFor({ photo_mode: { active: true }, chat_id: '1', message_id: 9 })).toEqual({ chatId: '1', deliveryKey: '9' })
  expect(m.photoRequestFor({ photo_mode: { active: false }, chat_id: '1', message_id: 9 })).toBeNull()
  expect(m.photoRequestFor({ photo_mode: { active: true }, photo_mode_off: true, chat_id: '1', message_id: 9 })).toBeNull()
  expect(m.photoRequestFor({ photo_mode: { active: true }, chat_id: '1' })).toBeNull()
})

// ─── 漫画模式：选 meta、端点、标记、失败照常投递 ─────────────────────────────
const TMP = mkdtempSync(join(tmpdir(), 'wm-comic-marks-'))
afterAll(() => rmSync(TMP, { recursive: true, force: true }))
const PHOTO = { active: true, opened_at_ms: 1, expires_at_ms: 2 }
const COMIC = { active: true, opened_at_ms: 1, expires_at_ms: 2 }
const COMIC_MARK = (n: number) =>
  `[auto-comic] 第${n}轮：本轮回复按 image 技能出三页漫画，第一页第三人称、第二页 user 的第一视角、第三页你自己的第一视角，user 不在场时只出第一页和第三页；写法按 novelai-skill 的「漫画写法」；正文不写分镜、画格或生图过程。`

/** 写一批同 chat 的收件件，返回一个能跑 pump 的 mgr；calls 记调了哪个端点，delivered 收投递正文 */
function pumpMgr(metas: Record<string, unknown>[], round: (path: string) => number | null | Promise<number | null>) {
  const m = mgr()
  const dir = mkdtempSync(join(TMP, 'inbox-'))
  m.queue = metas.map((meta, i) => {
    const path = join(dir, `123456789_${i + 1}.json`)
    writeFileSync(path, JSON.stringify({ chat_id: '123456789', message_id: i + 1, text: `t${i + 1}`, ...meta }))
    return { kind: 'file', path }
  })
  m.pumping = false; m.inFlight = false; m.phaseVal = 'running'
  const calls: Array<{ path: string; chatId: string; key: string }> = []
  m.modeRound = async (path: string, chatId: string, key: string) => { calls.push({ path, chatId, key }); return round(path) }
  let resolve!: (s: string) => void
  const delivered = new Promise<string>(r => { resolve = r })
  m.finishFileDelivery = (_plan: unknown, _merged: unknown, content: string) => resolve(content)
  return { m, calls, delivered }
}

test('lastModeMeta：取最后一条带模式指令者，都没有取首条', () => {
  const m = mgr()
  const a = { message_id: 1, comic_mode: COMIC }, b = { message_id: 2 }, c = { message_id: 3, photo_mode: PHOTO }
  expect(m.lastModeMeta([a, b, c])).toBe(c)
  expect(m.lastModeMeta([c, a, b])).toBe(a)
  const off = { message_id: 4, comic_mode_off: true }
  expect(m.lastModeMeta([a, off, b])).toBe(off)
  expect(m.lastModeMeta([b, { message_id: 5, photo_mode: { active: false } }])).toBe(b)
})

test('comicRequestFor：仅 active 且非 off 且有 delivery_key；合并件用全部 message_id', () => {
  const m = mgr()
  expect(m.comicRequestFor({ comic_mode: COMIC, chat_id: '1', message_id: 9 })).toEqual({ chatId: '1', deliveryKey: '9' })
  expect(m.comicRequestFor({ comic_mode: COMIC, chat_id: '1', merged_message_ids: ['7', '8'] })).toEqual({ chatId: '1', deliveryKey: '7,8' })
  expect(m.comicRequestFor({ comic_mode: { active: false }, chat_id: '1', message_id: 9 })).toBeNull()
  expect(m.comicRequestFor({ comic_mode: COMIC, comic_mode_off: true, chat_id: '1', message_id: 9 })).toBeNull()
  expect(m.comicRequestFor({ comic_mode: COMIC, chat_id: '1' })).toBeNull()
  expect(m.comicRequestFor({ photo_mode: PHOTO, chat_id: '1', message_id: 9 })).toBeNull()
})

test('同批重投不增轮：同一批选中的 meta 与投递键不变，计轮函数同 key 幂等', () => {
  const m = mgr()
  const batch = () => [{ chat_id: '1', message_id: 1 }, { chat_id: '1', message_id: 2, comic_mode: COMIC }, { chat_id: '1', message_id: 3 }]
  const k1 = m.comicRequestFor(m.lastModeMeta(batch()))
  const k2 = m.comicRequestFor(m.lastModeMeta(batch()))
  expect(k1).toEqual({ chatId: '1', deliveryKey: '2' })
  expect(k2).toEqual(k1)
  const once = nextPhotoRound(triggerPhotoState('1', '2', 1000), k1.deliveryKey, 2000)
  expect(nextPhotoRound(once, k2.deliveryKey, 3000)).toBe(once)
  expect(once.rounds).toBe(1)
})

test('pump：漫画开着 → 只调 /comic_round 一次，标记插在场景标注之前', async () => {
  const { m, calls, delivered } = pumpMgr([{ comic_mode: COMIC }, {}], () => 3)
  m.pump()
  const content = await delivered
  expect(calls).toEqual([{ path: '/comic_round', chatId: '123456789', key: '1' }])
  expect(content.split('[auto-comic]').length - 1).toBe(1)
  expect(content).toContain(COMIC_MARK(3))
  expect(content.indexOf('[auto-comic]')).toBeLessThan(content.indexOf('【私聊】'))
  expect(content).not.toContain('[auto-photo]')
})

test('pump：首条拍照、末条漫画 → 以末条为准调 /comic_round（拍照同口径改为最后一条获胜）', async () => {
  const { m, calls, delivered } = pumpMgr([{ photo_mode: PHOTO }, { comic_mode: COMIC }], () => 1)
  m.pump()
  const content = await delivered
  expect(calls.map(c => c.path)).toEqual(['/comic_round'])
  expect(calls[0]!.key).toBe('2')
  expect(content).toContain(COMIC_MARK(1))
})

test('pump：首条无模式键、末条拍照 → 仍调 /photo_round 并加拍照标记', async () => {
  const { m, calls, delivered } = pumpMgr([{}, { photo_mode: PHOTO }], () => 4)
  m.pump()
  const content = await delivered
  expect(calls.map(c => c.path)).toEqual(['/photo_round'])
  expect(content).toContain('[auto-photo] 第4轮')
  expect(content).not.toContain('[auto-comic]')
})

test('pump：末条是退出 → 不调端点、不加标记', async () => {
  const { m, calls, delivered } = pumpMgr([{ comic_mode: COMIC }, { comic_mode_off: true }], () => 1)
  m.pump()
  const content = await delivered
  expect(calls).toEqual([])
  expect(content).not.toContain('[auto-comic]')
})

test('pump：/comic_round 返回 null（失败）→ 照常投递、不带标记', async () => {
  const { m, delivered } = pumpMgr([{ comic_mode: COMIC }], () => null)
  m.pump()
  const content = await delivered
  expect(content).toContain('t1')
  expect(content).not.toContain('[auto-comic]')
})

test('modeRound：连不上 → 返回 null 并记 comic_round_failed reason=<错误名>', async () => {
  const m = mgr()
  m.channelDir = mkdtempSync(join(TMP, 'ch-'))
  writeFileSync(join(m.channelDir, '.api-token'), 'unit' + '0'.repeat(60))
  const lines: string[] = []
  const orig = process.stderr.write.bind(process.stderr)
  const origFetch = globalThis.fetch
  // 只替换传输层造"连不上"：本机代理环境下死端口可能回 502 或超时，结果不稳定
  ;(globalThis as any).fetch = async () => { throw Object.assign(new Error('connect failed'), { name: 'ConnectionRefused' }) }
  ;(process.stderr as any).write = (chunk: any, ...rest: any[]) => { lines.push(String(chunk)); return orig(chunk, ...rest) }
  try {
    expect(await m.modeRound('/comic_round', '123456789', 'k1')).toBeNull()
    expect(await m.modeRound('/photo_round', '123456789', 'k1')).toBeNull()
  } finally {
    ;(process.stderr as any).write = orig
    ;(globalThis as any).fetch = origFetch
  }
  expect(lines).toContain('worker-manager: comic_round_failed reason=ConnectionRefused\n')
  expect(lines).toContain('worker-manager: photo_round_failed reason=ConnectionRefused\n')
})
