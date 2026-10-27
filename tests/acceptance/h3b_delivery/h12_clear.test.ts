// H3b · H-12 清空与会话文件移动的互斥 —— 黑盒验收
// 只依据 .devflow/INTERFACE-h3b-delivery.md 二(3-6)。不读实现。
// 走完整消息处理路径（子进程驱动）。假进程 / 临时目录；绝不真起 claude、绝不碰真实会话。
// bug 修复模式：契约要求的 wm_clear_gate on/off 等可观测标记当前不存在 → 相关用例应为红。
import { test, expect } from 'bun:test'
import { runDriver, textUpdate, OWNER } from './_driverlib.ts'

const clearUpdate = () => textUpdate({ chatId: OWNER, fromId: OWNER, text: '/clear', messageId: 100 })
const afterUpdate = () => textUpdate({ chatId: OWNER, fromId: OWNER, text: '清空后的新消息X', messageId: 101 })

// 契约 6：可观测 —— 清空期间必须出现 wm_clear_gate on 与 wm_clear_gate off（且先 on 后 off）
test('清空_有gate_on与gate_off标记_且先开后关', async () => {
  const r = await runDriver({ scenario: 'clear', waitMs: 4000, updates: [clearUpdate(), afterUpdate()] }, 20000)
  const s = r.stderr.join('')
  expect(s.includes('wm_clear_gate on')).toBe(true)
  expect(s.includes('wm_clear_gate off')).toBe(true)
  expect(s.indexOf('wm_clear_gate on')).toBeLessThan(s.indexOf('wm_clear_gate off'))
}, 20000)

// 契约 5：清空流程结束后（含异常路径）必须释放门禁 —— 释放后新消息照常被处理（不得永久阻塞）
test('清空后_后续消息仍被处理_不被永久阻塞', async () => {
  const r = await runDriver({ scenario: 'clear', waitMs: 4000, updates: [clearUpdate(), afterUpdate()] }, 20000)
  const s = r.stderr.join('')
  // 清空后的新消息不得被永久阻塞：应看到它被路由/入队（回归护栏：现状即如此）
  expect(s.includes('清空后的新消息X') || s.includes('routed')).toBe(true)
}, 20000)
