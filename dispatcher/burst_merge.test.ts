// 黑盒验收：mergeInboxJsons（唯一依据 .devflow/INTERFACE-burst.md）。全假数据，不回显长正文。
import { test, expect } from 'bun:test'
import { mergeInboxJsons } from './burst_plan'

const raw = (o: Record<string, unknown>) => JSON.stringify({
  chat_id: 'FAKECHAT-1', message_id: 1, scene: 'private', ts: 1000, text: 'X', ...o,
})
const three = [
  raw({ message_id: 11, ts: 1000, text: 'A1' }),
  raw({ message_id: 12, ts: 2000, text: 'B2' }),
  raw({ message_id: 13, ts: 3000, text: 'C3' }),
]
const merged = (arr: string[]) => JSON.parse(mergeInboxJsons(arr))

test('顺序保持：三条正文按序以换行连接，逐字不改', () => {
  expect(merged(three).text).toBe('A1\nB2\nC3')
})

test('meta 取首条：chat_id / message_id / scene 来自第一条', () => {
  const r = merged([
    raw({ chat_id: 'FAKECHAT-1', message_id: 11, scene: 'private', text: 'A1' }),
    raw({ chat_id: 'FAKECHAT-9', message_id: 99, scene: 'group', text: 'B2' }),
  ])
  expect([r.chat_id, r.message_id, r.scene]).toEqual(['FAKECHAT-1', 11, 'private'])
})

test('ts 取末条', () => {
  expect(merged(three).ts).toBe(3000)
})

test('合并件带全部入站 message_id（party 绑定/拍照幂等用）', () => {
  expect(merged(three).merged_message_ids).toEqual(['11', '12', '13'])
  expect(merged([three[0]]).merged_message_ids).toBeUndefined()
})

test('返回值是字符串且为合法 JSON', () => {
  const s = mergeInboxJsons(three)
  expect(typeof s).toBe('string')
  expect(() => JSON.parse(s)).not.toThrow()
})

test('单条合并：正文与 ts 原样保留，不加分隔符', () => {
  const r = merged([raw({ ts: 7000, text: 'A1' })])
  expect([r.text, r.ts]).toEqual(['A1', 7000])
})

test('正文自带换行与 Unicode 逐字保留，不转义不裁剪', () => {
  const r = merged([raw({ text: '第一行\n第二行  ' }), raw({ text: '😀\t"引号"\\反斜杠' })])
  expect(r.text).toBe('第一行\n第二行  \n😀\t"引号"\\反斜杠')
})

test('空正文条目仍占一行，不被吞掉（反向）', () => {
  expect(merged([raw({ text: 'A1' }), raw({ text: '' }), raw({ text: 'C3' })]).text).toBe('A1\n\nC3')
})

test('长正文不丢字：长度与首尾字符正确（不回显正文）', () => {
  const long = 'z'.repeat(4000)
  const r = merged([raw({ text: long }), raw({ text: 'TAIL' })])
  expect(r.text.length).toBe(4005)
  expect([r.text.slice(0, 1), r.text.slice(-5)]).toEqual(['z', '\nTAIL'])
})

test('坏 JSON 在中间 → 抛 Error（非 TypeError，供调用方降级逐条投）', () => {
  let err: unknown
  try { mergeInboxJsons([three[0], '{not json', three[2]]) } catch (e) { err = e }
  expect(err).toBeInstanceOf(Error)
  expect(err).not.toBeInstanceOf(TypeError)
})

test('坏 JSON 在首条 → 同样抛 Error', () => {
  expect(() => mergeInboxJsons(['', three[1]])).toThrow(Error)
})

test('错误契约：空数组 → TypeError', () => {
  expect(() => mergeInboxJsons([])).toThrow(TypeError)
})

test('错误契约：入参非数组 → TypeError', () => {
  expect(() => mergeInboxJsons('{}' as any)).toThrow(TypeError)
})

test('错误契约：入参为 null → TypeError', () => {
  expect(() => mergeInboxJsons(null as any)).toThrow(TypeError)
})

test('错误契约：元素非字符串（对象）→ TypeError', () => {
  expect(() => mergeInboxJsons([{ text: 'A1' } as any, three[1]])).toThrow(TypeError)
})

test('错误契约：元素为 null → TypeError', () => {
  expect(() => mergeInboxJsons([three[0], null as any])).toThrow(TypeError)
})

// 模式键：同一批里最后一条带 photo_mode/photo_mode_off/comic_mode/comic_mode_off 者的四个键获胜，其余各条丢弃。
test('模式键取最后一条带模式指令者，拍照与漫画同口径', () => {
  const photo = { active: true, opened_at_ms: 1760000000000, expires_at_ms: 1760086400000 }
  const comic = { active: true, opened_at_ms: 1760000000001, expires_at_ms: 1760086400001 }
  const m = (...os: Record<string, unknown>[]) => merged(os.map(o => raw(o)))

  // a：首条 active:false 不算指令，第二条获胜
  const a = m({ message_id: 1, photo_mode: { active: false } }, { message_id: 2, photo_mode: photo })
  expect(a.photo_mode).toEqual(photo)
  // b：先开后关 → 只剩 off
  const b = m({ message_id: 1, photo_mode: photo }, { message_id: 2, photo_mode_off: true })
  expect(b.photo_mode).toBeUndefined()
  expect(b.photo_mode_off).toBe(true)
  // c：先关后开 → 只剩开（旧口径是关闭优先）
  const c = m({ message_id: 1, photo_mode_off: true }, { message_id: 2, photo_mode: photo })
  expect(c.photo_mode).toEqual(photo)
  expect(c.photo_mode_off).toBeUndefined()
  // d：后面的条目不带模式键 → 前一条的指令保留（首条不在模式里时也不丢）
  const d = m({ message_id: 1, text: '无模式键' }, { message_id: 2, photo_mode: photo }, { message_id: 3 })
  expect(d.photo_mode).toEqual(photo)
  // e：先拍照后漫画 → 只带漫画
  const e = m({ message_id: 1, photo_mode: photo }, { message_id: 2, comic_mode: comic }, { message_id: 3 })
  expect(e.comic_mode).toEqual(comic)
  expect(e.photo_mode).toBeUndefined()
  // f：获胜条同时带拍照与 comic_mode_off（只退漫画、拍照仍开）→ 两者都保留，旧的 comic_mode 丢弃
  const f = m({ message_id: 1, comic_mode: comic }, { message_id: 2, photo_mode: photo, comic_mode_off: true })
  expect(f.photo_mode).toEqual(photo)
  expect(f.comic_mode_off).toBe(true)
  expect(f.comic_mode).toBeUndefined()
  // g：整批没有模式指令 → 四个键都不出现
  const g = m({ message_id: 1, photo_mode: { active: false } }, { message_id: 2 })
  for (const k of ['photo_mode', 'photo_mode_off', 'comic_mode', 'comic_mode_off']) expect(k in g).toBe(false)
})
