// pub-comic · 投递标记：worker 实际收到的正文（行为清单 2,3,4 后半,7,14）
// 真 dispatcher 子进程（内含 worker-manager）+ 本机假 Telegram + 假 claude。
// 假 claude 只把收到的每行 stdin 记进临时文件并回 result 事件；这里读那个文件，看 bot 实际收到的正文。
import { test, expect, afterEach } from 'bun:test'
import { chmodSync } from 'node:fs'
import { dirname } from 'node:path'
import { CHATS, comicHarness, settle, stopAll } from './_comic_harness.ts'
import type { Comic } from './_comic_harness.ts'

afterEach(async () => { await stopAll() })

// 标记以"[auto-comic] 第n轮："定位；正文按接口文件行为 17：三页（第一页第三人称、第二页 user 的第一视角、第三页你自己的第一视角，user 不在场时只出第一页和第三页）
const MARK = (n: number) => `[auto-comic] 第${n}轮：`
const MARK_BODY_PARTS = ['三页', '第一页第三人称', '第二页 user 的第一视角', '第三页你自己的第一视角', 'user 不在场时只出第一页和第三页']
const count = (s: string, sub: string) => s.split(sub).length - 1
const waitDeliveries = (c: Comic, n: number, label: string) =>
  c.eventually(() => c.deliveries().length >= n, label, 60000)
const all = (c: Comic) => c.deliveries().join('\n----\n')

// ─── 行为 2 ───────────────────────────────────────────────────
test('漫画开启后首次投递带第1轮标记且在场景标注之前_再一次投递为第2轮', async () => {
  const c = await comicHarness({ fakeWorker: true })
  c.say('（漫画）')
  await waitDeliveries(c, 1, '首次投递')
  const d1 = c.deliveries()[0]
  expect(d1).toContain(MARK(1))
  const seg = d1.slice(d1.indexOf('[auto-comic]')).split('\n')[0]
  for (const part of MARK_BODY_PARTS) expect(seg).toContain(part)
  expect(seg).not.toContain('两页')
  expect(d1.indexOf(MARK(1))).toBeLessThan(d1.indexOf('【私聊】'))
  c.say('你好')
  await waitDeliveries(c, 2, '第二次投递')
  const d2 = c.deliveries()[1]
  expect(d2).toContain(MARK(2))
  expect(d2.indexOf(MARK(2))).toBeLessThan(d2.indexOf('【私聊】'))
  expect(d2).not.toContain(MARK(1))
}, 150000)

// ─── 行为 3 ───────────────────────────────────────────────────
test('burst合并的两条消息_只算一轮_整个投递里只出现一个auto_comic', async () => {
  const c = await comicHarness({ fakeWorker: true })
  c.burst(['（漫画）', '你好呀'])
  await c.eventually(() => c.deliveries().some(d => d.includes('你好呀')), '含第二条的投递', 60000)
  await settle(2500)
  const merged = c.deliveries().find(d => d.includes('你好呀'))!
  expect(merged.includes('（漫画）')).toBe(true)       // 前提：两条确实合并成了一次投递
  expect(count(all(c), '[auto-comic]')).toBe(1)
  expect(c.readJson(c.comicPath(CHATS[0])).rounds).toBe(1)
}, 150000)

// ─── 行为 4 后半 ─────────────────────────────────────────────────
test('退出后的投递不再带auto_comic', async () => {
  const c = await comicHarness({ fakeWorker: true })
  c.say('（漫画）')
  await waitDeliveries(c, 1, '首次投递')
  expect(c.deliveries()[0]).toContain('[auto-comic]')
  c.say('停')
  await waitDeliveries(c, 2, '退出那条的投递')
  c.say('你好')
  await waitDeliveries(c, 3, '退出之后的投递')
  expect(c.deliveries()[1]).not.toContain('[auto-comic]')
  expect(c.deliveries()[2]).not.toContain('[auto-comic]')
}, 180000)

// ─── 行为 7：同一批里触发与退出的先后 ───────────────────────────────────
function mergedWith(c: Comic, a: string, b: string): string | undefined {
  return c.deliveries().find(d => d.includes(a) && d.includes(b))
}

test('同批先漫画后拍照_合并件只标auto_photo_磁盘只剩拍照状态', async () => {
  const c = await comicHarness({ fakeWorker: true })
  c.burst(['（漫画）', '（拍照）'])
  await c.eventually(() => c.deliveries().some(d => d.includes('（拍照）')), '含拍照触发的投递', 60000)
  await settle(2000)
  const m = mergedWith(c, '（漫画）', '（拍照）')
  expect(m).toBeDefined()
  expect(m!).toContain('[auto-photo]')
  expect(all(c)).not.toContain('[auto-comic]')
  expect(c.has(c.photoPath(CHATS[0]))).toBe(true)
  expect(c.has(c.comicPath(CHATS[0]))).toBe(false)
}, 150000)

test('同批先拍照后漫画_合并件只标auto_comic_磁盘只剩漫画状态', async () => {
  const c = await comicHarness({ fakeWorker: true })
  c.burst(['（拍照）', '（漫画）'])
  await c.eventually(() => c.deliveries().some(d => d.includes('（漫画）')), '含漫画触发的投递', 60000)
  await settle(2000)
  const m = mergedWith(c, '（拍照）', '（漫画）')
  expect(m).toBeDefined()
  expect(m!).toContain('[auto-comic]')
  expect(all(c)).not.toContain('[auto-photo]')
  expect(c.has(c.comicPath(CHATS[0]))).toBe(true)
  expect(c.has(c.photoPath(CHATS[0]))).toBe(false)
}, 150000)

test('同批先漫画后停_合并件两种标记都没有_漫画状态已删', async () => {
  const c = await comicHarness({ fakeWorker: true })
  c.burst(['（漫画）', '停'])
  await c.eventually(() => c.deliveries().some(d => d.includes('停')), '含停的投递', 60000)
  await settle(2000)
  expect(mergedWith(c, '（漫画）', '停')).toBeDefined()
  expect(all(c)).not.toContain('[auto-comic]')
  expect(all(c)).not.toContain('[auto-photo]')
  expect(c.has(c.comicPath(CHATS[0]))).toBe(false)
}, 150000)

// ─── 行为 14：/comic_round 失败照常投递 ───────────────────────────────────
// 对外造 500：漫画开启后把该 bot 的漫画状态目录与状态文件改成只读，下一次投递时轮次写不进去，
// /comic_round 应回 500 state_write_failed。（"连不上"分支不在验收范围，见接口文件锁前补充。）
test('comic_round返回500_消息照常投递不带标记_日志comic_round_failed_status', async () => {
  const c = await comicHarness({ fakeWorker: true })
  const file = c.comicPath(CHATS[0])
  const dir = dirname(file)
  try {
    c.say('（漫画）')
    await c.stateAppears(file)
    await waitDeliveries(c, 1, '首次投递')
    chmodSync(file, 0o444)
    chmodSync(dir, 0o555)
    c.say('你好')
    await waitDeliveries(c, 2, '状态写不成时的投递')
    const d2 = c.deliveries()[1]
    expect(d2).toContain('你好')
    expect(d2).not.toContain('[auto-comic]')
    await c.eventually(() => c.h.stderr().includes('worker-manager: comic_round_failed status=500'),
      '日志 worker-manager: comic_round_failed status=500', 10000)
  } finally {
    try { chmodSync(dir, 0o755) } catch {}
    try { chmodSync(file, 0o644) } catch {}
  }
}, 150000)
