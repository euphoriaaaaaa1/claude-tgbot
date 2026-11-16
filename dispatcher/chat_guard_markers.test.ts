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
