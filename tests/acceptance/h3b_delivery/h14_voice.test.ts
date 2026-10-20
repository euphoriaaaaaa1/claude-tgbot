// H3b · H-14 语音与图片（先白名单、带超时、令牌不外发）—— 黑盒验收
// 只依据 .devflow/INTERFACE-h3b-delivery.md 三(7-9b,18)。不读实现。
// 走完整消息处理路径（子进程驱动 + 假 Telegram + 假转写服务），绝不发真请求、绝不真起进程。
// bug 修复模式：非白名单语音当前仍会调转写并泄露令牌、转写无超时 → 相关用例应为红。
import { test, expect } from 'bun:test'
import { runDriver, voiceUpdate, textUpdate, OWNER, NONOWNER } from './_driverlib.ts'

const BOT_TOKEN = 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK'

// 契约 7：非白名单发送者的语音 → 不调转写服务，记 voice_skipped reason=not_allowed
test('非白名单语音_不调转写_记voice_skipped', async () => {
  const r = await runDriver({
    scenario: 'voice',
    waitMs: 3000,
    updates: [voiceUpdate({ chatId: NONOWNER, fromId: NONOWNER })],
  })
  expect(r.transcribe.length).toBe(0)                                  // 绝不调转写
  const errText = r.stderr.join('')
  expect(errText.includes('voice_skipped')).toBe(true)
  expect(errText.includes('not_allowed')).toBe(true)
})

// 契约 9b：令牌不外发 —— 转写请求体里不得含 bot token（也不得含 token 字段）
test('白名单语音_转写请求体不含bot令牌', async () => {
  const r = await runDriver({
    scenario: 'voice',
    waitMs: 3000,
    updates: [voiceUpdate({ chatId: OWNER, fromId: OWNER })],
  })
  expect(r.transcribe.length).toBeGreaterThan(0)
  for (const t of r.transcribe) {
    expect(t.body.includes(BOT_TOKEN)).toBe(false)
    expect(t.body.includes('bot_token')).toBe(false)
    expect(t.body.includes('"token"')).toBe(false)
  }
})

// 契约 9：同一聊天"语音+文本"顺序与到达顺序一致（语音不得让后续文本抢跑）
test('同聊天_语音先于文本_按到达顺序处理', async () => {
  const r = await runDriver({
    scenario: 'voice',
    waitMs: 4000,
    transcribe: { text: '转写结果' },
    updates: [
      voiceUpdate({ chatId: OWNER, fromId: OWNER, messageId: 1 }),
      textUpdate({ chatId: OWNER, fromId: OWNER, text: '后续文本X', messageId: 2 }),
    ],
  })
  const lines = r.stderr.join('')
  const iVoice = lines.indexOf('转写结果')     // 语音（转写后）被处理
  const iText = lines.indexOf('后续文本X')
  expect(iVoice).toBeGreaterThanOrEqual(0)
  expect(iText).toBeGreaterThanOrEqual(0)
  expect(iVoice).toBeLessThan(iText)          // 语音先处理，文本不抢跑
})

// 契约 18/8：转写超时（VOICE_TIMEOUT_MS=20000）→ 记 voice_timeout 且给该聊天回一条固定文案
test('转写超时_记voice_timeout_且回执一次', async () => {
  const r = await runDriver({
    scenario: 'voice',
    waitMs: 24000,                 // 覆盖 20s 超时 + 余量
    transcribe: { delayMs: 25000 }, // 转写远超上界
    updates: [voiceUpdate({ chatId: OWNER, fromId: OWNER })],
  }, 40000)
  const errText = r.stderr.join('')
  expect(errText.includes('voice_timeout')).toBe(true)
  // 给该聊天回过一条消息（sendMessage 至少一次）
  expect(r.tgMethods.filter(m => m === 'sendMessage').length).toBeGreaterThanOrEqual(1)
}, 40000) // 单测超时须覆盖 20s 上界 + 驱动余量
