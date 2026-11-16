// pub-comic · 投递标记：worker 实际收到的正文（行为清单 2,3,4 后半,7,14）
// 真 dispatcher 子进程（内含 worker-manager）+ 本机假 Telegram + 假 claude。
// 假 claude 只把收到的每行 stdin 记进临时文件并回 result 事件；这里读那个文件，看 bot 实际收到的正文。
import { test, expect, afterEach } from 'bun:test'
import { CHATS, comicHarness, settle, stopAll } from './_comic_harness.ts'
import type { Comic } from './_comic_harness.ts'

afterEach(async () => { await stopAll() })

const MARK = (n: number) =>
  `[auto-comic] 第${n}轮：本轮回复按 image 技能出两页漫画，第一页第三人称、第二页 user 的第一视角，user 不在场时只出第一页；写法按 novelai-skill 的「漫画写法」；正文不写分镜、画格或生图过程。`
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
test('comic_round返回500_消息照常投递不带标记_日志comic_round_failed_status', async () => {
  const srv = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: () => new Response('{"ok":false}', { status: 500 }) })
  try {
    const c = await comicHarness({ fakeWorker: true, workerDispatcherUrl: `http://127.0.0.1:${srv.port}` })
    c.say('（漫画）')
    await waitDeliveries(c, 1, '首次投递')
    expect(c.deliveries()[0]).toContain('（漫画）')
    expect(c.deliveries()[0]).not.toContain('[auto-comic]')
    await c.eventually(() => /comic_round_failed status=500/.test(c.h.stderr()), '日志 comic_round_failed status=500', 10000)
  } finally { srv.stop(true) }
}, 150000)

test('comic_round连不上_消息照常投递不带标记_日志comic_round_failed_reason', async () => {
  const dead = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: () => new Response('') })
  const port = dead.port
  dead.stop(true)   // 释放端口：之后这里无人监听，连接会被拒
  const c = await comicHarness({ fakeWorker: true, workerDispatcherUrl: `http://127.0.0.1:${port}` })
  c.say('（漫画）')
  await waitDeliveries(c, 1, '首次投递')
  expect(c.deliveries()[0]).toContain('（漫画）')
  expect(c.deliveries()[0]).not.toContain('[auto-comic]')
  await c.eventually(() => /comic_round_failed reason=\S+/.test(c.h.stderr()), '日志 comic_round_failed reason=', 10000)
}, 150000)
