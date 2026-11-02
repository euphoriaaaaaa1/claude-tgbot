// pub-outage · 真人消息里的内部标记转义、/clear 作废补发（行为清单 #23 #24）
// 真 dispatcher 子进程 + 本机假 Telegram 推送。worker 用 PATH 上的 exit 97 桩，不起真 claude。
import { test, expect, afterEach } from 'bun:test'
import { PRIVATE_CHAT, startedPolling, stopAll, waitFor } from './_harness.ts'

afterEach(async () => { await stopAll() })

test('真人私聊里写了resend_check标记_进收件箱时开头方括号变全角', async () => {
  const h = await startedPolling()
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '看 [resend-check] 吧' })
  const item = await waitFor(
    () => h.inboxItems().find(i => i.data?.scene === 'private' && i.data?.is_bot_sender === false),
    30000, '真人私聊消息进入收件箱', 500)
  expect(item.data.text).toBe('看 ［resend-check] 吧')
}, 60000)

test('真人私聊里写了hang_check标记_同样转义_其它正文不动', async () => {
  const h = await startedPolling()
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '[hang-check] 你好 [momentum]' })
  const item = await waitFor(
    () => h.inboxItems().find(i => i.data?.scene === 'private' && i.data?.is_bot_sender === false),
    30000, '真人私聊消息进入收件箱', 500)
  expect(item.data.text).toBe('［hang-check] 你好 [momentum]')
}, 60000)

test('真人私聊发clear_回执发出之前清空代数已经加1', async () => {
  const rp: any = await import('../../../dispatcher/resend_plan')
  const h = await startedPolling()
  const before = rp.readClearEpoch(h.channel)
  let atReceipt: number | null = null
  h.fake.onMethod = (method: string) => {
    if (method === 'sendMessage' && atReceipt === null) atReceipt = rp.readClearEpoch(h.channel)
  }
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '/clear' })
  await waitFor(() => atReceipt !== null, 40000, '/clear 回执（sendMessage）发出', 200)
  expect(atReceipt!).toBeGreaterThan(before)
}, 90000)
