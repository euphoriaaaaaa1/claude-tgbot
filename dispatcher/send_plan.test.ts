import { test, expect } from 'bun:test'
import { buildSendPlan } from './send_plan'

const kinds = (p: ReturnType<typeof buildSendPlan>) => p.plan.map(i => i.kind === 'file' ? `f${i.index}` : 't').join(',')

test('无标记：文字在前，图全部压尾（旧行为不变）', () => {
  const r = buildSendPlan('你好\n\n吃了吗', 2)
  expect(kinds(r)).toBe('t,f0,f1')
  expect(r.cleanText).toBe('你好\n\n吃了吗')
})

test('标记插中间：文-图-文', () => {
  const r = buildSendPlan('刚拍的\n\n[[图1]]\n\n好看吗', 1)
  expect(kinds(r)).toBe('t,f0,t')
})

test('无数字标记按顺序取图', () => {
  const r = buildSendPlan('a[[图]]b[[图]]c', 2)
  expect(kinds(r)).toBe('t,f0,t,f1,t')
})

test('两张图只点名一张：另一张压尾', () => {
  const r = buildSendPlan('a\n\n[[图2]]\n\nb', 2)
  expect(kinds(r)).toBe('t,f1,t,f0')
})

test('越界标记吞掉不发也不漏原文', () => {
  const r = buildSendPlan('a[[图5]]b', 1)
  expect(kinds(r)).toBe('t,t,f0')
  expect(r.cleanText).toBe('ab')
})

test('重复点名同一张只发一次', () => {
  const r = buildSendPlan('[[图1]]x[[图1]]', 1)
  expect(kinds(r)).toBe('f0,t')
})

test('纯标记无文字：只发图', () => {
  const r = buildSendPlan('[[图]]', 1)
  expect(kinds(r)).toBe('f0')
  expect(r.cleanText).toBe('')
})

test('图片写法与空格容忍', () => {
  const r = buildSendPlan('a[[图片 1]]b[[图 ]]c', 2)
  expect(kinds(r)).toBe('t,f0,t,f1,t')
})

test('cleanText 去标记且压掉多余空行', () => {
  const r = buildSendPlan('a\n\n[[图1]]\n\nb', 1)
  expect(r.cleanText).toBe('a\n\nb')
})

test('文本为空只有图：不产生空文字项', () => {
  const r = buildSendPlan('', 2)
  expect(kinds(r)).toBe('f0,f1')
})
