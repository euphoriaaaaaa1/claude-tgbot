// 黑盒验收：time_annotate.ts 的 buildTimePrefix。时区一律显式传，绝不依赖机器时区。
import { test, expect } from 'bun:test'
import { buildTimePrefix } from './time_annotate'

const OPTS = { timeZone: 'Asia/Shanghai', locale: 'zh-CN' }
const MIN = 60_000
/** 参数即"上海本地时刻"，2026-08-24 是周一 */
const at = (h: number, m = 0, day = 24) => Date.UTC(2026, 7, day, h - 8, m)
const MON2137 = at(21, 37)

test('buildTimePrefix_文档基准格式_逐字一致且以换行结尾', () => {
  expect(buildTimePrefix(MON2137, null, OPTS)).toBe('（现在：8/24 周一 晚上21:37）\n')
})

test('buildTimePrefix_没有上次记录_只给时刻行不给间隔行', () => {
  expect(buildTimePrefix(MON2137, null, OPTS)).not.toContain('【')
})

const PERIODS: [number, string][] = [[0, '凌晨'], [5, '早上'], [8, '上午'], [11, '中午'], [13, '下午'], [18, '晚上'], [23, '深夜']]
for (const [h, word] of PERIODS) {
  test(`buildTimePrefix_时段词边界_本地${h}点整_用${word}`, () => {
    expect(buildTimePrefix(at(h), null, OPTS)).toContain(word)
  })
}

test('buildTimePrefix_间隔恰好等于30分钟阈值_不加间隔行', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 30 * MIN, OPTS)).not.toContain('【')
})

test('buildTimePrefix_间隔刚过阈值一秒_加间隔行且用分钟措辞', () => {
  const s = buildTimePrefix(MON2137, MON2137 - 30 * MIN - 1000, OPTS)
  expect(s).toContain('【距你们上次说话已过去约 30 分钟】')
})

test('buildTimePrefix_间隔13小时_措辞为约13小时', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 13 * 60 * MIN, OPTS)).toContain('约 13 小时')
})

test('buildTimePrefix_间隔89分钟_仍用分钟档', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 89 * MIN, OPTS)).toMatch(/分钟】/)
})

test('buildTimePrefix_间隔90分钟_单位切到小时档', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 90 * MIN, OPTS)).toMatch(/小时】/)
})

test('buildTimePrefix_间隔整24小时_单位切到天档', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 24 * 60 * MIN, OPTS)).toContain('约 1 天')
})

test('buildTimePrefix_间隔48小时_约2天', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 48 * 60 * MIN, OPTS)).toContain('约 2 天')
})

test('buildTimePrefix_跨天_日期跟到新的一天且时段词正确', () => {
  const s = buildTimePrefix(at(0, 30, 25), at(23, 50, 24), OPTS)
  expect(s).toContain('8/25')
  expect(s).toContain('凌晨')
})

test('buildTimePrefix_跨天但间隔只有40分钟_仍按间隔判定加间隔行', () => {
  expect(buildTimePrefix(at(0, 30, 25), at(23, 50, 24), OPTS)).toContain('【')
})

test('buildTimePrefix_负间隔时钟倒流_只给时刻行不给间隔行', () => {
  const s = buildTimePrefix(MON2137, MON2137 + 5 * 60 * MIN, OPTS)
  expect(s).toContain('（现在：')
  expect(s).not.toContain('【')
})

test('buildTimePrefix_上次时间是NaN_只给时刻行', () => {
  expect(buildTimePrefix(MON2137, NaN, OPTS)).not.toContain('【')
})

test('buildTimePrefix_上次时间是Infinity_只给时刻行', () => {
  expect(buildTimePrefix(MON2137, Infinity, OPTS)).not.toContain('【')
})

test('buildTimePrefix_当前时间是NaN_返回空串不抛', () => {
  expect(buildTimePrefix(NaN, null, OPTS)).toBe('')
})

test('buildTimePrefix_当前时间是0_返回空串', () => {
  expect(buildTimePrefix(0, null, OPTS)).toBe('')
})

test('buildTimePrefix_当前时间为负_返回空串', () => {
  expect(buildTimePrefix(-1, null, OPTS)).toBe('')
})

test('buildTimePrefix_阈值传0_回落默认30分钟所以10分钟不加间隔行', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 10 * MIN, { ...OPTS, gapThresholdMin: 0 })).not.toContain('【')
})

test('buildTimePrefix_阈值传负数_回落默认30分钟', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 10 * MIN, { ...OPTS, gapThresholdMin: -5 })).not.toContain('【')
})

test('buildTimePrefix_阈值不是数字_回落默认30分钟', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 10 * MIN, { ...OPTS, gapThresholdMin: 'x' as any })).not.toContain('【')
})

test('buildTimePrefix_阈值调成1分钟_5分钟间隔就该标注', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 5 * MIN, { ...OPTS, gapThresholdMin: 1 })).toContain('【')
})

test('buildTimePrefix_带间隔行时也以换行结尾', () => {
  expect(buildTimePrefix(MON2137, MON2137 - 13 * 60 * MIN, OPTS).endsWith('\n')).toBe(true)
})

test('buildTimePrefix_时区参数生效_同一时刻不同时区输出不同', () => {
  const cn = buildTimePrefix(MON2137, null, OPTS)
  const utc = buildTimePrefix(MON2137, null, { ...OPTS, timeZone: 'UTC' })
  expect(utc).not.toBe(cn)
  expect(utc).toContain('下午')
})

test('buildTimePrefix_时区名非法_不抛异常', () => {
  expect(() => buildTimePrefix(MON2137, null, { ...OPTS, timeZone: 'Not/AZone' })).not.toThrow()
})

test('buildTimePrefix_不省略opts时也能跑_省略时区不抛', () => {
  expect(() => buildTimePrefix(MON2137, null)).not.toThrow()
})

test('buildTimePrefix_产物不含尖括号和chat_id_前置拼接不会破坏channel元数据', () => {
  const s = buildTimePrefix(MON2137, MON2137 - 13 * 60 * MIN, OPTS)
  expect(s).not.toMatch(/[<>]/)
  expect(s).not.toContain('chat_id')
})
