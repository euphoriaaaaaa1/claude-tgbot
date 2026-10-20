// 黑盒验收测试 · shouldRefresh / debounceGate —— 唯一依据 .devflow/INTERFACE-provider-sync.md
// 实现尚不存在，跑红属预期。纯函数，无文件/网络。
import { test, expect } from 'bun:test'
import { shouldRefresh, debounceGate } from './provider_sync'

const SIG_A = 'sig-fake-aaaa'
const SIG_B = 'sig-fake-bbbb'

// ===== shouldRefresh：四象限 =====
test('shouldRefresh_首次基线prev为空_不刷新', () => {
  expect(shouldRefresh('', SIG_A)).toBe(false)
})

test('shouldRefresh_next为空即文件消失或坏掉_不刷新', () => {
  expect(shouldRefresh(SIG_A, '')).toBe(false)
})

test('shouldRefresh_两个签名相等_不刷新', () => {
  expect(shouldRefresh(SIG_A, SIG_A)).toBe(false)
})

test('shouldRefresh_两个非空签名不等_刷新', () => {
  expect(shouldRefresh(SIG_A, SIG_B)).toBe(true)
})

test('shouldRefresh_两边都为空_不刷新', () => {
  expect(shouldRefresh('', '')).toBe(false)
})

// 反向用例：坏掉又恢复成同一个 provider，不该白重启
test('shouldRefresh_坏掉后恢复成原签名_不刷新', () => {
  expect(shouldRefresh(SIG_A, '')).toBe(false)
  expect(shouldRefresh('', SIG_A)).toBe(false)
})

test('shouldRefresh_签名只差一个字符_刷新', () => {
  expect(shouldRefresh('sig-fake-aaaa', 'sig-fake-aaab')).toBe(true)
})

// ---- 错误契约：任一入参非字符串 → TypeError ----
const NOT_STRINGS: [string, unknown][] = [
  ['null', null], ['undefined', undefined], ['数字', 1],
  ['对象', {}], ['数组', []], ['布尔', false],
]
for (const [label, v] of NOT_STRINGS) {
  test(`shouldRefresh_prev是${label}_抛TypeError`, () => {
    expect(() => shouldRefresh(v as any, SIG_A)).toThrow(TypeError)
  })
  test(`shouldRefresh_next是${label}_抛TypeError`, () => {
    expect(() => shouldRefresh(SIG_A, v as any)).toThrow(TypeError)
  })
}

// ===== debounceGate：窗口边界 =====
// 语义推导：窗口内(diff < window) → false 被压掉；diff >= window → true 立即生效。
// 边界取 >= 是由契约「windowMs<=0 按 0 处理(不防抖)」反推——window=0 且 diff=0 必须为 true。
test('debounceGate_窗口内的第二次事件_被压掉', () => {
  expect(debounceGate(1000, 1500, 1200)).toBe(false)
})

test('debounceGate_窗口外的事件_立即生效', () => {
  expect(debounceGate(1000, 3000, 1200)).toBe(true)
})

test('debounceGate_间隔恰好等于窗口_立即生效', () => {
  expect(debounceGate(1000, 2200, 1200)).toBe(true)
})

test('debounceGate_间隔比窗口少1毫秒_被压掉', () => {
  expect(debounceGate(1000, 2199, 1200)).toBe(false)
})

test('debounceGate_窗口为0_同一毫秒也立即生效', () => {
  expect(debounceGate(1000, 1000, 0)).toBe(true)
})

test('debounceGate_窗口为负_按0处理不防抖', () => {
  expect(debounceGate(1000, 1000, -500)).toBe(true)
  expect(debounceGate(1000, 1001, -500)).toBe(true)
})

test('debounceGate_窗口内重复调用_结果一致不受调用次数影响', () => {
  expect(debounceGate(1000, 1500, 1200)).toBe(false)
  expect(debounceGate(1000, 1500, 1200)).toBe(false)
  expect(debounceGate(1000, 1500, 1200)).toBe(false)
})

test('debounceGate_时刻为0_按正常数值处理', () => {
  expect(debounceGate(0, 0, 1200)).toBe(false)
  expect(debounceGate(0, 1200, 1200)).toBe(true)
})

test('debounceGate_极大时间戳_不溢出仍正确', () => {
  expect(debounceGate(Number.MAX_SAFE_INTEGER - 1200, Number.MAX_SAFE_INTEGER, 1200)).toBe(true)
})

// 时钟回拨：now < last → diff 为负 < window → 压掉。契约未明写，见 TEST-PLAN 歧义节
test('debounceGate_时钟回拨now早于last_被压掉', () => {
  expect(debounceGate(5000, 1000, 1200)).toBe(false)
})

// ---- 错误契约：nowMs / lastMs 非有限数 → TypeError ----
const NOT_FINITE: [string, unknown][] = [
  ['NaN', NaN], ['Infinity', Infinity], ['负Infinity', -Infinity],
  ['字符串', '1000'], ['null', null], ['undefined', undefined], ['对象', {}],
]
for (const [label, v] of NOT_FINITE) {
  test(`debounceGate_lastMs是${label}_抛TypeError`, () => {
    expect(() => debounceGate(v as any, 2000, 1200)).toThrow(TypeError)
  })
  test(`debounceGate_nowMs是${label}_抛TypeError`, () => {
    expect(() => debounceGate(1000, v as any, 1200)).toThrow(TypeError)
  })
  // windowMs 非有限数同样抛 TypeError（裁决 C 补充）；注意负窗口是有限数，仍按 0 处理不抛
  test(`debounceGate_windowMs是${label}_抛TypeError`, () => {
    expect(() => debounceGate(1000, 2000, v as any)).toThrow(TypeError)
  })
}
