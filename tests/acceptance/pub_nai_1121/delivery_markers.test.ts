// pub-nai-1121 · 行为 17：dispatcher 投递给 bot 的系统标记正文
//   [auto-photo]：3 张（旁观摄影机视角、user 的第一视角、你自己的第一视角）
//   [auto-comic]：三页（第一页第三人称、第二页 user 的第一视角、第三页你自己的第一视角，user 不在场时只出第一页和第三页）
// 沿用 pub_comic 的夹具：真 dispatcher 子进程 + 本机假 Telegram + 假 claude（只记 worker 收到的每行 stdin）。零副作用，不联网。
import { test, expect, afterEach } from 'bun:test'
import { comicHarness, stopAll } from '../pub_comic/_comic_harness.ts'
import type { Comic } from '../pub_comic/_comic_harness.ts'

afterEach(async () => { await stopAll() })

/** 从标记起到该行结束的那一段 */
function markerLine(delivery: string, marker: string): string {
  const i = delivery.indexOf(marker)
  expect(i >= 0 ? 'found' : `投递正文里没有 ${marker}：${delivery.slice(0, 300)}`).toBe('found')
  const rest = delivery.slice(i)
  const nl = rest.indexOf('\n')
  return nl >= 0 ? rest.slice(0, nl) : rest
}
const firstDelivery = async (c: Comic) => {
  await c.eventually(() => c.deliveries().length >= 1, '首次投递', 60000)
  return c.deliveries()[0]
}

test('拍照开启后首次投递_auto_photo标记写3张_旁观摄影机_user第一视角_自己第一视角', async () => {
  const c = await comicHarness({ fakeWorker: true })
  c.say('（拍照）')
  const seg = markerLine(await firstDelivery(c), '[auto-photo]')
  for (const part of ['3 张', '旁观摄影机视角', 'user 的第一视角', '你自己的第一视角']) expect(seg).toContain(part)
  expect(seg).not.toContain('两张')
}, 150000)

test('漫画开启后首次投递_auto_comic标记写三页_第一页第三人称_第二页user第一视角_第三页自己第一视角_user不在场只出一三页', async () => {
  const c = await comicHarness({ fakeWorker: true })
  c.say('（漫画）')
  const seg = markerLine(await firstDelivery(c), '[auto-comic]')
  for (const part of ['三页', '第一页第三人称', '第二页 user 的第一视角', '第三页你自己的第一视角', 'user 不在场时只出第一页和第三页']) {
    expect(seg).toContain(part)
  }
  expect(seg).not.toContain('两页')
}, 150000)
