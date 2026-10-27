// H4b · H-20 时区可配置（黑盒验收，bug 修复模式：现在应为红）
//
// 只依据 .devflow/{BRIEF,INTERFACE}-h4b-path-perf.md。不读实现。
// 契约要求「单一解析函数」+ 全部"现在几点"调用点改用它。buildTimePrefix 为本仓既有真实
// 导出（时间格式化调用点），用于验证「调用点确实走了统一解析」。
// 契约未固定解析函数名，按别名回退；返回形状为时区名字符串。
//
// 跑法：bun test tests/acceptance/h4b_path_perf/h20_timezone.test.ts
import { test, expect, afterEach } from 'bun:test'
import { pickOrThrow, timeAnnotate } from './_fixture.ts'

const SYS_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone

afterEach(() => { delete process.env.BOT_TZ })

// 契约入口：单一时区解析函数（名未定，别名回退）。签名假定：resolve({env,access,system})。
const resolveTz = (): ((o?: Record<string, unknown>) => string) =>
  pickOrThrow(timeAnnotate, ['resolveTimeZone', 'resolveTz', 'pickTimeZone', 'getTimeZone', 'resolveTimezone', 'timeZone'])

const buildTimePrefix = (): ((ts: number, prev: number | null, o?: unknown) => string) =>
  pickOrThrow(timeAnnotate, ['buildTimePrefix'])

// 2026-08-24(周一) 上海 21:37 == UTC 13:37
const MON2137 = Date.UTC(2026, 7, 24, 13, 37)

// ═══ §11：优先级 BOT_TZ > access.json.timezone > 系统 ════════════════════════

test('H20_优先级_BOT_TZ压过access与系统', () => {
  process.env.BOT_TZ = 'Asia/Shanghai'
  const got = resolveTz()({
    env: { BOT_TZ: 'Asia/Shanghai' },
    access: { timezone: 'Asia/Tokyo' },
    system: 'America/New_York',
  })
  expect(got).toBe('Asia/Shanghai')
})

test('H20_优先级_无BOT_TZ_先取access的timezone_再回落系统', () => {
  delete process.env.BOT_TZ
  const fn = resolveTz()
  expect(fn({ env: {}, access: { timezone: 'Asia/Tokyo' }, system: 'America/New_York' })).toBe('Asia/Tokyo')
  expect(fn({ env: {}, access: {}, system: SYS_TZ })).toBe(SYS_TZ)
})

// ═══ §11：调用点确实用了统一解析函数（以 buildTimePrefix 的时区行为验证）══════

test('H20_调用点_设BOT_TZ=Asia/Shanghai后_时间格式化按该时区而非默认时区', () => {
  process.env.BOT_TZ = 'Asia/Shanghai'
  const fn = buildTimePrefix()
  const noTz = fn(MON2137, null, { locale: 'zh-CN' })          // 故意不传 timeZone
  const explicitSH = fn(MON2137, null, { locale: 'zh-CN', timeZone: 'Asia/Shanghai' })
  const explicitUTC = fn(MON2137, null, { locale: 'zh-CN', timeZone: 'UTC' })
  // 走了统一解析 → 无 tz 参数时应等于显式上海时区的结果（且不同于 UTC）
  expect(noTz).toBe(explicitSH)
  expect(noTz).not.toBe(explicitUTC)
  expect(noTz).toContain('晚上')
})
