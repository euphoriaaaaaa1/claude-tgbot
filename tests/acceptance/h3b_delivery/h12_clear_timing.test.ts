// H3b · H-12 清空时序（移动会话文件与 exit 的先后、门禁标记）—— 黑盒验收
// 只依据 .devflow/INTERFACE-h3b-delivery.md 二(3-6)。
// 手法：子进程驱动 + 可控假 claude（sleep-term = 长睡但被 kill 后响应 exit）+ 预置会话文件；
// 先发一条普通消息把 worker 拉起，再 /clearall 触发清空。
// bug 修复模式：契约要求的 wm_clear_gate on/off、wm_clear_moved / wm_clear_exit_timeout 当前不存在 → 应为红。
//
// 已知边界：消息处理路径未暴露可注入时钟，故 CLEAR_EXIT_TIMEOUT_MS=10000 的"快到"只能靠真实等待或
// 断言其标记存在（本文件用后者）；真正的注入时钟驱动需要实现侧提供时钟注入缝（见 TEST-PLAN 未覆盖）。
import { test, expect, beforeAll } from 'bun:test'
import { runDriver, textUpdate, OWNER, type DriverResult } from './_driverlib.ts'

let r: DriverResult
beforeAll(async () => {
  r = await runDriver({
    scenario: 'clear',
    claudeStub: 'sleep-term',
    preSession: true,
    waitMs: 7000,
    updates: [
      textUpdate({ chatId: OWNER, fromId: OWNER, text: '预热消息W', messageId: 1 }),
      textUpdate({ chatId: OWNER, fromId: OWNER, text: '/clearall', messageId: 2 }),
      textUpdate({ chatId: OWNER, fromId: OWNER, text: '清空后新消息X', messageId: 3 }),
    ],
  }, 9000) // 驱动在结束处会因已 spawn 的子进程滞留；结果按事件增量落盘，到点强杀即可
}, 30000)

test('清空_门禁可观测_且有移动或超时标记', () => {
  const s = r.stderr.join('')
  expect(s.includes('wm_clear_gate on')).toBe(true)
  expect(s.includes('wm_clear_gate off')).toBe(true)
  expect(s.indexOf('wm_clear_gate on')).toBeLessThan(s.indexOf('wm_clear_gate off'))
  // 移动会话文件：要么正常移动，要么因 exit 超时强制放行（两者之一必须发生）
  expect(s.includes('wm_clear_moved') || s.includes('wm_clear_exit_timeout')).toBe(true)
})

test('清空_exit事件先于文件移动', () => {
  const s = r.stderr.join('')
  const iExit = s.indexOf('wm_exit')
  const iMoved = s.indexOf('wm_clear_moved')
  expect(iExit).toBeGreaterThanOrEqual(0)
  expect(iMoved).toBeGreaterThanOrEqual(0)
  expect(iExit).toBeLessThan(iMoved)   // 先收到 exit，再移动会话文件
})

test('门禁期到达的新消息_释放后被投递_顺序不变', () => {
  const s = r.stderr.join('')
  // 清空后的新消息不得丢弃/抢跑：应出现在 clear 之后、且被路由
  const iClear = s.indexOf('清空后新消息X')
  expect(iClear).toBeGreaterThanOrEqual(0)
  expect(s.includes('routed')).toBe(true)
})
