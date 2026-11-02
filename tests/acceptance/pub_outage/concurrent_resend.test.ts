// pub-outage · 三个真 dispatcher 并发的补发端到端（行为清单 #25）
// 长用例：单文件 240 秒口径，用例显式给 280 秒超时。三只 bot 各用自己的临时目录和假 Telegram。
import { test, expect, afterEach } from 'bun:test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { PRIVATE_CHAT, sleep, startedPolling, stopAll, type Harness } from './_harness.ts'

afterEach(async () => { await stopAll() })

const TWO_SEGMENTS = `ALPHA-SECRET-1 ${'甲'.repeat(3000)}\n\nBETA-SECRET-2 ${'乙'.repeat(3000)}`

/** 按 message_id 去重（同一个件可能同时出现在暂存区和正式区） */
function resendItems(h: Harness): any[] {
  const byId = new Map<string, any>()
  for (const i of h.inboxItems()) {
    if (String(i.data?.text ?? '').startsWith('[resend-check]')) byId.set(String(i.data.message_id), i.data)
  }
  return [...byId.values()]
}

test('三只bot并发_恢复后只投一次_停用不投_clear作废', async () => {
  const [a, b, c] = await Promise.all([
    startedPolling({ bot: 'outbot1' }), startedPolling({ bot: 'outbot2' }), startedPolling({ bot: 'outbot3' }),
  ])
  await Promise.all([a, b, c].map(async h => {
    await h.makeLinkDown()
    const r = await h.send({ chat_id: PRIVATE_CHAT, text: TWO_SEGMENTS })
    expect(r.json.resend_parts).toEqual([0, 1])
  }))

  // ② 先停用补发，再恢复
  mkdirSync(b.disabled, { recursive: true })
  writeFileSync(join(b.disabled, `resend-${b.bot}`), '')
  const t0 = Date.now()
  await a.bringLinkUp()
  await b.bringLinkUp()
  await c.bringLinkUp()
  // ③ 恢复后立刻由假服务推一条真人 /clear
  c.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '/clear' })

  // 150 秒窗口
  while (Date.now() - t0 < 150_000) {
    await sleep(1000)
    if (resendItems(a).length >= 1 && Date.now() - t0 > 140_000) break
  }
  while (Date.now() - t0 < 150_000) await sleep(500)

  // ① 恰好一个补发件，内容正确
  const itemsA = resendItems(a)
  expect(itemsA.length).toBe(1)
  expect(itemsA[0].synthetic).toBe(true)
  expect(itemsA[0].chat_type).toBe('private')
  expect(String(itemsA[0].text).split('\n').filter(l => /^\d+\. /.test(l)).length).toBe(2)
  expect(a.stderr()).toContain(`resend_inject chat=${PRIVATE_CHAT} round=1/3 parts=2`)

  // ② 停用：没有补发件、没有 resend_inject
  expect(resendItems(b)).toEqual([])
  expect(b.stderr()).not.toContain('resend_inject')

  // ③ /clear 作废：没有补发件，日志记 resend_void reason=session
  expect(resendItems(c)).toEqual([])
  expect(c.stderr()).toContain(`resend_void chat=${PRIVATE_CHAT} reason=session`)

  // ① 再等 20 秒仍只有 1 个
  await sleep(20_000)
  expect(resendItems(a).length).toBe(1)
}, 280000)
