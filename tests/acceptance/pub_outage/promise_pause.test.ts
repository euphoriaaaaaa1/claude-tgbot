// pub-outage · 约定在线路不通期间顺延；恢复后还有 30 分钟起床缓冲，缓冲期内来一条真人私聊入站就不再等（行为清单 #8，锁后修订第 2 条）
// 长用例：单条显式给长超时。真 dispatcher 子进程 + 本机假 Telegram。
import { test, expect, afterEach } from 'bun:test'
import { PRIVATE_CHAT, startedPolling, stopAll, waitFor, type Harness } from './_harness.ts'

afterEach(async () => { await stopAll() })

function promiseDueItems(h: Harness): any[] {
  return h.inboxItems().filter(i => String(i.data?.text ?? '').includes('[promise-due]'))
}

/** 不通期间登记一条 5 秒后到点的约定，等到日志记下暂停，并确认此时 inbox 里没有 [promise-due] */
async function pausedWithPromise(): Promise<Harness> {
  const h = await startedPolling()
  await h.makeLinkDown()
  const reg = await h.post('/promise', { action: 'set', chat_id: PRIVATE_CHAT, gist: '给花浇水', due_at: Date.now() + 5000 })
  expect(reg.status).toBe(200)
  expect(reg.json.ok).toBe(true)
  await h.waitStderr('PAUSED gate=link_down', 100000)
  expect(promiseDueItems(h)).toEqual([])
  return h
}

/** 恢复线路，并等到假服务确实收到恢复后的第一次长轮询（证明线路已通） */
async function bringUpAndConfirm(h: Harness): Promise<void> {
  await h.bringLinkUp()
  const since = Date.now()
  await waitFor(() => h.fake.callsOf('getUpdates').some(c => c.at >= since), 60000, '恢复后假服务收到 getUpdates', 500)
}

test('约定到点时线路不通_不注入且记PAUSED_恢复后来一条真人私聊_150秒内注入', async () => {
  const h = await pausedWithPromise()
  await bringUpAndConfirm(h)

  // 起床缓冲期内来一条真人私聊入站，就不再等
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '在吗' })

  const item = await waitFor(() => promiseDueItems(h)[0], 150000, '真人私聊入站后 inbox 出现 [promise-due] 件', 1000)
  expect(String(item.data.text)).toContain('[promise-due]')
  expect(String(item.data.chat_id)).toBe(PRIVATE_CHAT)
}, 330000)

test('约定到点时线路不通_恢复后没有真人消息_150秒内不注入_证明是顺延不是丢了', async () => {
  const h = await pausedWithPromise()
  await bringUpAndConfirm(h)

  await new Promise(r => setTimeout(r, 150000))
  expect(promiseDueItems(h)).toEqual([])
}, 330000)
