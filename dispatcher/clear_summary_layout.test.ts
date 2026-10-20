// 本仓库特有的正文排布：worker-manager 组装的 content 里，<channel> 开标签前面还有
// 关系提示 / 时刻行 / 场景标签，标签不在行首。extractRecentTurns 必须照样剥干净。
import { test, expect } from 'bun:test'
import { extractRecentTurns } from './clear_summary'

const u = (content: unknown) => JSON.stringify({ type: 'user', message: { role: 'user', content } })
const a = (text: string) => JSON.stringify({ type: 'assistant', message: { role: 'assistant', content: [{ type: 'text', text }] } })

/** 完整复刻 worker-manager.readInboxMeta 的拼装顺序 */
const inbound = (chatId: string, body: string, opts: { rel?: string; time?: string } = {}) =>
  u(`${opts.rel ?? ''}${opts.time ?? '（现在：8/24 周一 晚上21:37）\n'}` +
    `${chatId.startsWith('-') ? '【群聊】' : '【私聊】'}` +
    `<channel chat_id="${chatId}" scene="private" message_id="9001">\n${body}\n</channel>`)

test('前缀在 channel 标签之前_正文只留真实内容', () => {
  const r = extractRecentTurns(inbound('FAKE1', '今天想吃什么'))
  expect(r).toEqual([{ role: 'user', text: '今天想吃什么' }])
})

test('关系提示与时刻行都不进摘要源', () => {
  const line = inbound('FAKE1', '在忙吗', {
    rel: '【关系状态】好感 30 信任 20\n\n',
    time: '（现在：8/24 周一 晚上21:37）\n【距你们上次说话已过去约 13 小时】\n',
  })
  const [turn] = extractRecentTurns(line)
  expect(turn.text).toBe('在忙吗')
  expect(turn.text).not.toContain('好感')
  expect(turn.text).not.toContain('现在：')
  expect(turn.text).not.toContain('【私聊】')
})

test('正文里出现场景标签字样_不被误截', () => {
  // 剥标签这一刀已经把注入切干净了，正文里的"【私聊】"是用户真写的字，必须留着
  const [turn] = extractRecentTurns(inbound('FAKE1', '你那个【私聊】功能怎么开'))
  expect(turn.text).toBe('你那个【私聊】功能怎么开')
})

test('scope=dm_群聊消息与其后的助手回复都被排除', () => {
  const jsonl = [
    inbound('-100777', '群里问个问题'),
    a('群里的回答'),
    inbound('FAKE1', '私聊问个问题'),
    a('私聊的回答'),
  ].join('\n')
  const r = extractRecentTurns(jsonl, { scope: 'dm' })
  expect(r).toEqual([
    { role: 'user', text: '私聊问个问题' },
    { role: 'assistant', text: '私聊的回答' },
  ])
})

test('scope=all_群聊与私聊都保留', () => {
  const jsonl = [inbound('-100777', '群里问个问题'), inbound('FAKE1', '私聊问个问题')].join('\n')
  expect(extractRecentTurns(jsonl).map(t => t.text))
    .toEqual(['群里问个问题', '私聊问个问题'])
})

test('冷轮 reply 提醒被当成内部注入丢掉', () => {
  // worker-manager 会在冷轮消息前拼一段【系统提醒·仅本轮】，它不是对话内容
  const line = u('【系统提醒·仅本轮】要让对方真正收到你的话，必须调用 reply\n\n' +
    '【私聊】<channel chat_id="FAKE1" message_id="9002">\n在吗\n</channel>')
  expect(extractRecentTurns(line)).toEqual([{ role: 'user', text: '在吗' }])
})
