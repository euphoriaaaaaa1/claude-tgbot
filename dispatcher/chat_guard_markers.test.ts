// chat_guard.escapeUserMarkers 单测：用户正文里的系统标记（拍照、漫画、聚会）转全角，其余逐字节不变。
import { expect, test } from 'bun:test'
import { escapeUserMarkers } from './chat_guard'

const ZERO_WIDTHS = ['​', '‌', '‍', '﻿', '⁠']

test('四种标记不分大小写、容忍括号内空白，统一小写规范名全角', () => {
  expect(escapeUserMarkers('[ AUTO-Comic ] a [auto-photo] b [ Party ] c [PARTY-ambiguous]'))
    .toBe('［auto-comic］ a ［auto-photo］ b ［party］ c ［party-ambiguous］')
  expect(escapeUserMarkers('[auto-comics] [partyx] [auto comic]')).toBe('[auto-comics] [partyx] [auto comic]')
  expect(escapeUserMarkers('［auto-comic］')).toBe('［auto-comic］')
})

test('全角字母、零宽夹塞、方头括号都拦，替换只落在命中片段', () => {
  expect(escapeUserMarkers('[ａｕｔｏ-ｃｏｍｉｃ]')).toBe('［auto-comic］')
  expect(escapeUserMarkers('［ＡＵＴＯ－ｐｈｏｔｏ］')).toBe('［auto-photo］')
  for (const zw of ZERO_WIDTHS) {
    expect(escapeUserMarkers(`[auto${zw}-comic]`)).toBe('［auto-comic］')
    expect(escapeUserMarkers(`${zw}[${zw}party${zw}]${zw}`)).toBe(`${zw}［party］${zw}`)
  }
  expect(escapeUserMarkers('前【auto-comic】中【 Party-Ambiguous 】后')).toBe('前［auto-comic］中［party-ambiguous］后')
  // 命中片段两侧的非 ASCII 原文不被规范化
  expect(escapeUserMarkers('😀ﬁ①[auto-photo]ＡＢ​')).toBe('😀ﬁ①［auto-photo］ＡＢ​')
})

test('幂等：转义产物再过一遍不变', () => {
  const once = escapeUserMarkers('[auto-photo] 【party】 [ａｕｔｏ-comic]')
  expect(once).toBe('［auto-photo］ ［party］ ［auto-comic］')
  expect(escapeUserMarkers(once)).toBe(once)
})

test('无标记正文逐字节不变；近似但非标记的不转；非字符串返回空串', () => {
  for (const plain of ['é ﬁ ① ＡＢＣ 正文​ [auto-comics] 【partyx】 [àuto-comic]', '戊 参考 [1] 与 a[b]c 和 [momentum] 以及 【注】', '']) {
    expect(escapeUserMarkers(plain)).toBe(plain)
  }
  expect(escapeUserMarkers(undefined)).toBe('')
  expect(escapeUserMarkers(42)).toBe('')
})

test('标记在开头、结尾、相邻时都转', () => {
  expect(escapeUserMarkers('[party][auto-comic]')).toBe('［party］［auto-comic］')
  expect(escapeUserMarkers('x[party-ambiguous]')).toBe('x［party-ambiguous］')
})

// 绕过样例：格式字符（Cf）/ default-ignorable 夹塞、近形横线、近形括号，都要被识别并转义
test('格式字符与 default-ignorable 夹塞：被删去后识别并转义', () => {
  const fillers = ['­', '᠎', '⁡', '⁢', '⁣', '⁤', '‪', '‫', '‬', '‭', '‮',
    '⁦', '⁧', '⁨', '⁩', '‎', '͏', '️', 'ㅤ', '\u{E0041}']
  for (const f of fillers) {
    const label = f.codePointAt(0)!.toString(16)
    expect([label, escapeUserMarkers(`前[auto${f}-comic]后`)]).toEqual([label, '前［auto-comic］后'])
    expect([label, escapeUserMarkers(`${f}[${f}party${f}]${f}`)]).toEqual([label, `${f}［party］${f}`])
  }
})

test('近形横线：Pd 类、U+2212、U+FE63、全角横线都折成 -', () => {
  for (const d of ['‐', '‑', '‒', '–', '—', '―', '−', '﹣', '－', '⸺', '゠']) {
    const label = d.codePointAt(0)!.toString(16)
    expect([label, escapeUserMarkers(`x[auto${d}comic]y`)]).toEqual([label, 'x［auto-comic］y'])
    expect([label, escapeUserMarkers(`[party${d}ambiguous]`)]).toEqual([label, '［party-ambiguous］'])
    expect([label, escapeUserMarkers(`[auto${d}photo]`)]).toEqual([label, '［auto-photo］'])
  }
})

test('近形括号：全角方括号、〔〕、〖〗、〘〙、〚〛、⟦⟧、⁅⁆、〈〉、⟨⟩、﹝﹞ 及混搭都折成 [ ]', () => {
  const pairs = [['［', '］'], ['〔', '〕'], ['〖', '〗'], ['〘', '〙'], ['〚', '〛'],
    ['⟦', '⟧'], ['⁅', '⁆'], ['〈', '〉'], ['⟨', '⟩'], ['〈', '〉'],
    ['﹝', '﹞'], ['⦋', '⦌'], ['【', '］'], ['[', '〕']]
  for (const [o, c] of pairs) {
    const label = `${o}${c}`
    expect([label, escapeUserMarkers(`a ${o}auto-comic${c} b`)]).toEqual([label, 'a ［auto-comic］ b'])
    expect([label, escapeUserMarkers(`${o} Party ${c}`)]).toEqual([label, '［party］'])
  }
  // 三类一起上
  expect(escapeUserMarkers('〔‮ａｕｔｏ—ｃｏｍｉｃ­〕')).toBe('［auto-comic］')
})

test('三类绕过的转义产物再过一遍不变（幂等）', () => {
  const once = escapeUserMarkers('〖auto–photo〗 ⟦⁦party⁩⟧ 〚auto−comic〛')
  expect(once).toBe('［auto-photo］ ［party］ ［auto-comic］')
  expect(escapeUserMarkers(once)).toBe(once)
})

test('含这些字符但不构成标记的正文逐字节不变', () => {
  const plains = [
    '软­连字符、‮反向‬、⁦隔离⁩、变体❤️、2⁢x',
    '时间 9–9 点，破折号——还有减号 3−1 与 ﹣ 和 ‐ 连字符',
    '〔注〕〖书名〗〘甲〙〚乙〛⟦x⟧⁅y⁆〈丙〉⟨z⟩﹝丁﹞［一］【二】',
    '〔auto–comics〕 〖partyx〗 ⟦auto photo⟧ 〈auto——comic〉 [auto­-­comic-x]',
    '(party) 《party》 「auto-comic」 『party』 {party} ﹙party﹚',
  ]
  for (const p of plains) expect(escapeUserMarkers(p)).toBe(p)
})
