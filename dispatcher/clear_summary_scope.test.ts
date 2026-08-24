// 黑盒验收：clear_summary.ts 的 extractRecentTurns 的 scope 选项（2026-08-24 新裁决）
import { test, expect } from 'bun:test'
import { extractRecentTurns } from './clear_summary'

const DM = 'FAKEDM-1'
const GRP = '-FAKEGRP-9'
const cu = (chatId: string, text: string) =>
  JSON.stringify({ type: 'user', origin: { kind: 'channel' }, message: { role: 'user', content: `<channel chat_id="${chatId}" message_id="1">${text}</channel>` } })
const asst = (text: string) =>
  JSON.stringify({ type: 'assistant', message: { role: 'assistant', content: [{ type: 'text', text }] } })

// 群私交错，且开头有一条没有归属的孤儿助手行
const MIXED = [
  asst('孤儿回复'),
  cu(DM, '私聊问题'), asst('私聊回答'),
  cu(GRP, '群里问题'), asst('群里回答'),
  cu(DM, '私聊追问'), asst('私聊再答'),
].join('\n')
const texts = (opts?: any) => extractRecentTurns(MIXED, opts).map(t => t.text)

test('scope为dm_群私交错_只留私聊用户行和归属私聊的助手行', () => {
  expect(extractRecentTurns(MIXED, { scope: 'dm' })).toEqual([
    { role: 'user', text: '私聊问题' },
    { role: 'assistant', text: '私聊回答' },
    { role: 'user', text: '私聊追问' },
    { role: 'assistant', text: '私聊再答' },
  ])
})

test('scope为dm_开头的孤儿助手行_因为前面没有任何channel用户行被丢弃', () => {
  expect(texts({ scope: 'dm' })).not.toContain('孤儿回复')
})

test('scope为dm_群聊用户行被丢弃', () => {
  expect(texts({ scope: 'dm' })).not.toContain('群里问题')
})

test('scope为dm_跟在群聊后面的助手行按最近归属判定_被丢弃', () => {
  expect(texts({ scope: 'dm' })).not.toContain('群里回答')
})

test('不传scope_与显式传all完全一致', () => {
  expect(extractRecentTurns(MIXED)).toEqual(extractRecentTurns(MIXED, { scope: 'all' }))
})

test('只传maxTurns不传scope_仍与all一致_不会误开启过滤', () => {
  expect(extractRecentTurns(MIXED, { maxTurns: 10 })).toEqual(extractRecentTurns(MIXED, { maxTurns: 10, scope: 'all' }))
})

test('scope为all_群聊和孤儿助手行全部保留', () => {
  expect(texts({ scope: 'all' })).toEqual(['孤儿回复', '私聊问题', '私聊回答', '群里问题', '群里回答', '私聊追问', '私聊再答'])
})

test('scope是非法值group_按all处理', () => {
  expect(extractRecentTurns(MIXED, { scope: 'group' as any })).toEqual(extractRecentTurns(MIXED, { scope: 'all' }))
})

test('scope是空串_按all处理', () => {
  expect(extractRecentTurns(MIXED, { scope: '' as any })).toEqual(extractRecentTurns(MIXED, { scope: 'all' }))
})

test('scope是null_按all处理且不抛', () => {
  expect(extractRecentTurns(MIXED, { scope: null as any })).toEqual(extractRecentTurns(MIXED, { scope: 'all' }))
})

test('scope是数字_按all处理且不抛', () => {
  expect(extractRecentTurns(MIXED, { scope: 1 as any })).toEqual(extractRecentTurns(MIXED, { scope: 'all' }))
})

test('全是群聊内容_scope为dm_返回空数组', () => {
  const j = [cu(GRP, '群消息一'), asst('群回复一'), cu(GRP, '群消息二'), asst('群回复二')].join('\n')
  expect(extractRecentTurns(j, { scope: 'dm' })).toEqual([])
})

test('只有孤儿助手行没有任何channel用户行_scope为dm_返回空数组', () => {
  expect(extractRecentTurns([asst('甲'), asst('乙')].join('\n'), { scope: 'dm' })).toEqual([])
})

test('私聊行后连续多条助手行_scope为dm_全部保留', () => {
  const j = [cu(DM, '问'), asst('答一'), asst('答二')].join('\n')
  expect(extractRecentTurns(j, { scope: 'dm' }).map(t => t.text)).toEqual(['问', '答一', '答二'])
})

test('群聊行之后又回到私聊_归属重新切回私聊_助手行保留', () => {
  const j = [cu(GRP, '群问'), asst('群答'), cu(DM, '私问'), asst('私答')].join('\n')
  expect(extractRecentTurns(j, { scope: 'dm' }).map(t => t.text)).toEqual(['私问', '私答'])
})

test('scope为dm叠加maxTurns_结果里绝不混入群聊内容', () => {
  const out = extractRecentTurns(MIXED, { scope: 'dm', maxTurns: 1 }).map(t => t.text)
  expect(out.length).toBeLessThanOrEqual(1)
  for (const t of out) expect(['私聊问题', '私聊回答', '私聊追问', '私聊再答']).toContain(t)
})

test('scope为dm时channel标签照样被剥掉_正文不残留chat_id', () => {
  const out = extractRecentTurns(MIXED, { scope: 'dm' })
  for (const t of out) expect(t.text).not.toContain('chat_id')
})

test('空输入配scope为dm_返回空数组不抛', () => {
  expect(extractRecentTurns('', { scope: 'dm' })).toEqual([])
})
