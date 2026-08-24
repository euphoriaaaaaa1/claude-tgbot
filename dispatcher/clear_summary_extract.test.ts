// 黑盒验收：clear_summary.ts 的 extractRecentTurns
import { test, expect } from 'bun:test'
import { extractRecentTurns } from './clear_summary'

const u = (content: unknown) => JSON.stringify({ type: 'user', message: { role: 'user', content } })
const a = (...blocks: unknown[]) => JSON.stringify({ type: 'assistant', message: { role: 'assistant', content: blocks } })
const txt = (text: string) => ({ type: 'text', text })
const NORMAL = u('今天想吃什么')

test('extractRecentTurns_一问一答_返回两轮且时间正序', () => {
  const r = extractRecentTurns(`${NORMAL}\n${a(txt('吃面'))}`)
  expect(r).toEqual([
    { role: 'user', text: '今天想吃什么' },
    { role: 'assistant', text: '吃面' },
  ])
})

test('extractRecentTurns_channel标签被剥掉_只留正文', () => {
  const r = extractRecentTurns(u('<channel chat_id="FAKECHAT-1" message_id="9001">今天想吃什么</channel>'))
  expect(r).toEqual([{ role: 'user', text: '今天想吃什么' }])
})

test('extractRecentTurns_剥标签后正文里不残留chat_id等元数据', () => {
  const r = extractRecentTurns(u('<channel chat_id="FAKECHAT-1" message_id="9001">你好</channel>'))
  expect(r[0].text).not.toContain('chat_id')
  expect(r[0].text).not.toContain('<channel')
})

const NOISE = [
  '[self-initiate] 该主动说点什么了',
  '[director] 这轮轮到你说',
  '[peer-inbound] 隔壁 bot 说了句话',
  '[moment-post] 去发条朋友圈',
  '[memory-compactor] 记忆维护完成',
  '[系统自检] 一切正常',
  'Continue from where you left off.',
  '[Request interrupted by user]',
  '<command-name>/clear</command-name>',
]
for (const n of NOISE) {
  test(`extractRecentTurns_丢弃内部注入行_${n.slice(0, 16)}`, () => {
    const r = extractRecentTurns(`${u(n)}\n${NORMAL}`)
    expect(r).toEqual([{ role: 'user', text: '今天想吃什么' }])
  })
}

test('extractRecentTurns_助手消息里的thinking和tool_use块被丢弃_只留文本', () => {
  const r = extractRecentTurns(a({ type: 'thinking', thinking: '内部推理' }, { type: 'tool_use', name: 'Bash', input: {} }, txt('好的')))
  expect(r).toEqual([{ role: 'assistant', text: '好的' }])
})

test('extractRecentTurns_只有tool_use没有文本_不产出轮次', () => {
  expect(extractRecentTurns(a({ type: 'tool_use', name: 'Bash', input: {} }))).toEqual([])
})

test('extractRecentTurns_空文本消息_不产出轮次', () => {
  expect(extractRecentTurns(`${u('')}\n${u('   ')}`)).toEqual([])
})

test('extractRecentTurns_输入空串_返回空数组', () => {
  expect(extractRecentTurns('')).toEqual([])
})

test('extractRecentTurns_整篇乱码_返回空数组且不抛', () => {
  expect(extractRecentTurns('{{{坏行\n乱码乱码\n}\n')).toEqual([])
})

test('extractRecentTurns_乱码混在中间_跳过坏行仍取出正常轮次', () => {
  expect(extractRecentTurns(`坏行\n{半个json\n${NORMAL}\n\n`)).toEqual([{ role: 'user', text: '今天想吃什么' }])
})

test('extractRecentTurns_clear后只剩系统注入无真实对话_返回空数组', () => {
  expect(extractRecentTurns(`{"type":"summary","summary":"x"}\n${u('Continue from where you left off.')}`)).toEqual([])
})

test('extractRecentTurns_maxTurns限制_只保留最近的两轮且仍是正序', () => {
  const jsonl = ['t1', 't2', 't3', 't4', 't5'].map(u).join('\n')
  expect(extractRecentTurns(jsonl, { maxTurns: 2 }).map(t => t.text)).toEqual(['t4', 't5'])
})

test('extractRecentTurns_maxTurns为0_回落默认不返回空', () => {
  const jsonl = ['t1', 't2', 't3'].map(u).join('\n')
  expect(extractRecentTurns(jsonl, { maxTurns: 0 }).map(t => t.text)).toEqual(['t1', 't2', 't3'])
})

test('extractRecentTurns_maxTurns为负_回落默认', () => {
  const jsonl = ['t1', 't2', 't3'].map(u).join('\n')
  expect(extractRecentTurns(jsonl, { maxTurns: -5 })).toHaveLength(3)
})

test('extractRecentTurns_单行超长_截断到maxChars且不抛', () => {
  const r = extractRecentTurns(u('字'.repeat(500)), { maxChars: 50 })
  expect(r).toHaveLength(1)
  expect(r[0].text.length).toBeLessThanOrEqual(50)
})

test('extractRecentTurns_不传opts_使用默认值不抛', () => {
  expect(() => extractRecentTurns(NORMAL)).not.toThrow()
})

test('extractRecentTurns_content是纯字符串或块数组两种形态都能解析', () => {
  const r = extractRecentTurns(`${u('文字型')}\n${a(txt('数组型'))}`)
  expect(r.map(t => t.text)).toEqual(['文字型', '数组型'])
})
