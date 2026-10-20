// T18 worker-manager 投递标记：party/拍照标记必须落在场景标注之前（clear_summary 按场景标注切注入），
// 且用户正文里的同名方括号已由 buildContent 转全角。白盒，不起 claude。
import { test, expect } from 'bun:test'
import { ROOT } from '../acceptance/desync/_env.ts'
import { join } from 'path'

process.env.CHANNEL_DIR ??= join(ROOT, 'channels', 'wm-marks')
const { WorkerManager } = await import('../../worker-manager')

function mgr() {
  const m = Object.create(WorkerManager.prototype) as any
  m.buildContent = (meta: any) => `【私聊】<channel chat_id="${meta.chat_id}">\n${meta.text}\n</channel>`
  return m
}

test('party 标记插在场景标注之前', () => {
  const m = mgr()
  const out = m.buildDeliveryContent([{ chat_id: '100001', text: 'hi', party_chat_id: '-100123' }])
  expect(out).toContain('[party] 线下聚会进行中（群 -100123）')
  expect(out.indexOf('[party]')).toBeLessThan(out.indexOf('【私聊】'))
})

test('多个群聚会 → party-ambiguous 标记', () => {
  const m = mgr()
  const out = m.buildDeliveryContent([{ chat_id: '100001', text: 'hi', party_ambiguous: true }])
  expect(out).toContain('[party-ambiguous]')
  expect(out).not.toContain('[party] 线下')
})

test('拍照标记同样插在场景标注之前', () => {
  const m = mgr()
  const base = m.buildDeliveryContent([{ chat_id: '100001', text: 'hi' }])
  const out = m.injectDeliveryMarks(base, ['[auto-photo] 第2轮：x'])
  expect(out.indexOf('[auto-photo]')).toBeLessThan(out.indexOf('【私聊】'))
})

test('photoRequestFor：仅 active 且非 off 且有 delivery_key', () => {
  const m = mgr()
  expect(m.photoRequestFor({ photo_mode: { active: true }, chat_id: '1', message_id: 9 })).toEqual({ chatId: '1', deliveryKey: '9' })
  expect(m.photoRequestFor({ photo_mode: { active: false }, chat_id: '1', message_id: 9 })).toBeNull()
  expect(m.photoRequestFor({ photo_mode: { active: true }, photo_mode_off: true, chat_id: '1', message_id: 9 })).toBeNull()
  expect(m.photoRequestFor({ photo_mode: { active: true }, chat_id: '1' })).toBeNull()
})
