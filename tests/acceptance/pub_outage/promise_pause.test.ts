// pub-outage · 约定在线路不通期间顺延、恢复后照常触发（行为清单 #8）
// 长用例：单条显式给 240 秒超时。真 dispatcher 子进程 + 本机假 Telegram。
import { test, expect, afterEach } from 'bun:test'
import { PRIVATE_CHAT, startedPolling, stopAll, waitFor, type Harness } from './_harness.ts'

afterEach(async () => { await stopAll() })

function promiseDueItems(h: Harness): any[] {
  return h.inboxItems().filter(i => String(i.data?.text ?? '').includes('[promise-due]'))
}

test('约定到点时线路不通_不注入且记PAUSED_恢复后150秒内照常注入', async () => {
  const h = await startedPolling()
  await h.makeLinkDown()
  const reg = await h.post('/promise', { action: 'set', chat_id: PRIVATE_CHAT, gist: '给花浇水', due_at: Date.now() + 5000 })
  expect(reg.status).toBe(200)
  expect(reg.json.ok).toBe(true)

  // 不通期间：至少经过一次约定巡检，日志记暂停，inbox 没有 [promise-due]
  await h.waitStderr('PAUSED gate=link_down', 100000)
  expect(promiseDueItems(h)).toEqual([])

  // 恢复后 150 秒内出现
  await h.bringLinkUp()
  const item = await waitFor(() => promiseDueItems(h)[0], 150000, '恢复后 inbox 出现 [promise-due] 件', 1000)
  expect(String(item.data.text)).toContain('[promise-due]')
  expect(String(item.data.chat_id)).toBe(PRIVATE_CHAT)
}, 240000)
