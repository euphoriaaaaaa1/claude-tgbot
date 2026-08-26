// 黑盒验收：renderHangPrompt。只依据 .devflow/INTERFACE-hang.md。
import { test, expect } from 'bun:test'
import { renderHangPrompt } from './hang_plan'

const KINDS = ['followup1', 'followup2', 'memoryWrite', 'lateReaction'] as const
// test.each 的类型要一个可变数组，as const 出来的只读元组过不了本仓 tsc 严格档。
// 只是换个同值的容器，用例与断言一字不变。
const KIND_ROWS = [...KINDS]

test.each(KIND_ROWS)('%s_输出以[hang-check]开头(burst不合并的识别前缀)', (kind) => {
  expect(renderHangPrompt(kind, 12, '上课')).toStartWith('[hang-check]')
})

test.each(KIND_ROWS)('%s_输出含未回时长与活动名', (kind) => {
  const s = renderHangPrompt(kind, 42, '在健身房')
  expect(s).toContain('42')
  expect(s).toContain('在健身房')
})

test('四种kind文本互不相同(不是同一段套话)', () => {
  const all = KINDS.map(k => renderHangPrompt(k, 7, '写代码'))
  expect(new Set(all).size).toBe(4)
})

test.each(KIND_ROWS)('%s_只输出一小段内部文本_不夹带正文', (kind) => {
  const s = renderHangPrompt(kind, 7, '写代码')
  expect(s.length).toBeLessThanOrEqual(200)
  expect(s.split('[hang-check]').length - 1).toBe(1)
})

test('活动名含中文空格emoji与特殊字符_原样出现', () => {
  const name = '午睡 💤 (勿扰) <b>'
  expect(renderHangPrompt('lateReaction', 1, name)).toContain(name)
})

test('时长为极大值1440分钟_正常渲染', () => {
  expect(renderHangPrompt('followup1', 1440, '出差')).toContain('1440')
})

// ── 错误契约 ──────────────────────────────────────────────────────
test.each(['followup3', '', 'FOLLOWUP1', null, undefined, 1])('kind非法(%p)_抛TypeError', (k) => {
  expect(() => renderHangPrompt(k as never, 7, '写代码')).toThrow(TypeError)
})

test.each([NaN, Infinity, '7', null, undefined, {}])('minutesSilent非法(%p)_抛TypeError', (m) => {
  expect(() => renderHangPrompt('followup1', m as never, '写代码')).toThrow(TypeError)
})

test('minutesSilent为负数_抛TypeError', () => {
  expect(() => renderHangPrompt('followup1', -1, '写代码')).toThrow(TypeError)
})

test.each([[null], [undefined], [42], [{}], [['上课']]])('activityName非字符串(%p)_抛TypeError', (a) => {
  expect(() => renderHangPrompt('followup1', 7, a as never)).toThrow(TypeError)
})
