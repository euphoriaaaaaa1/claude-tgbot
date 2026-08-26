// 黑盒验收：situ_anchor.ts 的 formatSituLine（入站消息的"她此刻的情境锚点"）。
// 纯函数，无 IO、无时钟；夹具全部自造，不碰真配置。
import { test, expect } from 'bun:test'
import { formatSituLine } from './situ_anchor'
import { extractRecentTurns } from './clear_summary'

// ─── 四种组合的逐字格式 ────────────────────────────────────────────
test('两者都空_返回空串_不产生空锚点行', () => {
  expect(formatSituLine('', '')).toBe('')
})

test('只有活动_只报活动', () => {
  expect(formatSituLine('', '备课')).toBe('（你此刻：备课）\n')
})

test('只有学期_只报学期', () => {
  expect(formatSituLine('暑假', '')).toBe('（你此刻：暑假）\n')
})

test('两者都有_学期在前活动在后_中间用空格间隔号', () => {
  expect(formatSituLine('提前返校期', '在学校备课')).toBe('（你此刻：提前返校期 · 在学校备课）\n')
})

// ─── 边界 ────────────────────────────────────────────────────────
test('纯空白当空处理_不生成只有分隔符的锚点', () => {
  expect(formatSituLine('   ', '\t\n ')).toBe('')
})

test('一侧纯空白_退化成单项_不留下悬空的间隔号', () => {
  expect(formatSituLine('  ', '午睡')).toBe('（你此刻：午睡）\n')
  expect(formatSituLine('寒假', '   ')).toBe('（你此刻：寒假）\n')
})

test('两侧空白被裁掉_不出现全角括号里贴着空格', () => {
  expect(formatSituLine(' 暑假 ', ' 追剧 ')).toBe('（你此刻：暑假 · 追剧）\n')
})

test('恒以单个换行结尾_不多不少', () => {
  const s = formatSituLine('暑假', '追剧')
  expect(s.endsWith('）\n')).toBe(true)
  expect(s.endsWith('\n\n')).toBe(false)
})

test('活动名含标点或英文_原样保留不转义', () => {
  expect(formatSituLine('', 'Zoom 会议（第 2 场）')).toBe('（你此刻：Zoom 会议（第 2 场））\n')
})

test('同样入参恒等输出_纯函数无状态', () => {
  expect(formatSituLine('暑假', '追剧')).toBe(formatSituLine('暑假', '追剧'))
})

// ─── 错误契约：非字符串一律抛 TypeError（静默吞会让锚点无声消失，更难查）───
const BAD: [string, unknown][] = [
  ['null', null], ['undefined', undefined], ['number', 0], ['boolean', false],
  ['object', { toString: () => '暑假' }], ['array', ['暑假']],
]
for (const [label, v] of BAD) {
  test(`termLabel 是 ${label}_抛 TypeError`, () => {
    expect(() => formatSituLine(v as any, '备课')).toThrow(TypeError)
  })
  test(`activityName 是 ${label}_抛 TypeError`, () => {
    expect(() => formatSituLine('暑假', v as any)).toThrow(TypeError)
  })
}

test('缺参调用_抛 TypeError_不静默当空串', () => {
  expect(() => (formatSituLine as any)()).toThrow(TypeError)
  expect(() => (formatSituLine as any)('暑假')).toThrow(TypeError)
})

test('报错信息带参数名_不回显入参值', () => {
  expect(() => formatSituLine(null as any, '备课')).toThrow(/termLabel/)
  expect(() => formatSituLine('暑假', 42 as any)).toThrow(/activityName/)
})

// ─── 与既有注入层的兼容：锚点行必须拼在场景标注之前，才会被摘要剥干净 ────
test('锚点行拼在场景标注之前_摘要仍只拿到真实正文', () => {
  // 样例按本仓 worker-manager 的组装形态：注入前缀在外、<channel> 标签紧贴正文
  const content = `（现在：8/26 周三 上午09:30）\n${formatSituLine('提前返校期', '在学校备课')}`
    + `【私聊】<channel chat_id="FAKEDM" message_id="1">\n你在干嘛\n</channel>`
  const jsonl = JSON.stringify({ type: 'user', message: { role: 'user', content } })
  expect(extractRecentTurns(jsonl).map(t => t.text)).toEqual(['你在干嘛'])
})
