// chat_guard.escapeUserMarkers 单测：用户正文里的系统标记（拍照、漫画、聚会）转全角，其余逐字节不变。
import { expect, test } from 'bun:test'
import { escapeUserMarkers } from './chat_guard'

const ZERO_WIDTHS = ['\u200B', '\u200C', '\u200D', '\uFEFF', '\u2060']

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
  expect(escapeUserMarkers('😀ﬁ①[auto-photo]ＡＢ\u200B')).toBe('😀ﬁ①［auto-photo］ＡＢ\u200B')
})

test('幂等：转义产物再过一遍不变', () => {
  const once = escapeUserMarkers('[auto-photo] 【party】 [ａｕｔｏ-comic]')
  expect(once).toBe('［auto-photo］ ［party］ ［auto-comic］')
  expect(escapeUserMarkers(once)).toBe(once)
})

test('无标记正文逐字节不变；近似但非标记的不转；非字符串返回空串', () => {
  for (const plain of ['é ﬁ ① ＡＢＣ 正文\u200B [auto-comics] 【partyx】', '戊 参考 [1] 与 a[b]c 和 [momentum] 以及 【注】', '']) {
    expect(escapeUserMarkers(plain)).toBe(plain)
  }
  // 带声调的近形字母去掉组合附加符后同样识别（比对副本先 NFD 去 Mn）
  expect(escapeUserMarkers('x [àuto-comic] y')).toBe('x ［auto-comic］ y')
  expect(escapeUserMarkers(undefined)).toBe('')
  expect(escapeUserMarkers(42)).toBe('')
})

test('标记在开头、结尾、相邻时都转', () => {
  expect(escapeUserMarkers('[party][auto-comic]')).toBe('［party］［auto-comic］')
  expect(escapeUserMarkers('x[party-ambiguous]')).toBe('x［party-ambiguous］')
})

// 绕过样例：格式字符（Cf）/ default-ignorable 夹塞、近形横线、近形括号，都要被识别并转义
test('格式字符与 default-ignorable 夹塞：被删去后识别并转义', () => {
  const fillers = ['\u00AD', '\u180E', '\u2061', '\u2062', '\u2063', '\u2064', '\u202A', '\u202B', '\u202C', '\u202D', '\u202E',
    '\u2066', '\u2067', '\u2068', '\u2069', '\u200E', '\u034F', '\uFE0F', '\u3164', '\u{E0041}']
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
  expect(escapeUserMarkers('〔\u202Eａｕｔｏ—ｃｏｍｉｃ\u00AD〕')).toBe('［auto-comic］')
})

test('三类绕过的转义产物再过一遍不变（幂等）', () => {
  const once = escapeUserMarkers('〖auto–photo〗 ⟦\u2066party\u2069⟧ 〚auto−comic〛')
  expect(once).toBe('［auto-photo］ ［party］ ［auto-comic］')
  expect(escapeUserMarkers(once)).toBe(once)
})

test('含这些字符但不构成标记的正文逐字节不变', () => {
  const plains = [
    '软\u00AD连字符、\u202E反向\u202C、\u2066隔离\u2069、变体❤\uFE0F、2\u2062x',
    '时间 9–9 点，破折号——还有减号 3−1 与 ﹣ 和 ‐ 连字符',
    '〔注〕〖书名〗〘甲〙〚乙〛⟦x⟧⁅y⁆〈丙〉⟨z⟩﹝丁﹞［一］【二】',
    '〔auto–comics〕 〖partyx〗 ⟦auto photo⟧ 〈auto——comic〉 [auto\u00AD-\u00ADcomic-x]',
    '(party) 《party》 「auto-comic」 『party』 {party} ﹙party﹚',
  ]
  for (const p of plains) expect(escapeUserMarkers(p)).toBe(p)
})

// 审计样例：近形字母、未列入的括号、非 Pd 横线
test('西里尔、希腊、小型大写近形字母折成拉丁后识别并转义', () => {
  const cases: [string, string][] = [
    ['[р\u0430rty]', '［party］'],
    ['[\u0430ut\u043E-\u0441\u043Emic]', '［auto-comic］'],
    ['[\u03A1ΑRTY]', '［party］'],
    ['[Α\u03C5\u03C4\u03BF-\u03F2\u03BF\u039C\u0399\u03F2]', '［auto-comic］'],
    ['[auto-р\u04BB\u043Et\u043E]', '［auto-photo］'],
    ['[party-\u0430\u041Cв\u0456\u0261u\u03BF\u03C5s]', '［party-ambiguous］'],
    ['[\u1D18\u1D00\u0280\u1D1B\u028F]', '［party］'],
    ['[party-amb\u0131guou\uA731]', '［party-ambiguous］'],
  ]
  for (const [src, want] of cases) expect([src, escapeUserMarkers(src)]).toEqual([src, want])
})

test('Ps/Pe 整类括号折成 [ ]：❲❳、⟬⟭、⦗⦘、⸢⸣ 等', () => {
  for (const [o, c] of [['❲', '❳'], ['⟬', '⟭'], ['⦗', '⦘'], ['⸢', '⸣'], ['❨', '❩'], ['〝', '〞']]) {
    const label = `${o}${c}`
    expect([label, escapeUserMarkers(`甲${o}party${c}乙`)]).toEqual([label, '甲［party］乙'])
    expect([label, escapeUserMarkers(`${o}auto-comic${c}`)]).toEqual([label, '［auto-comic］'])
  }
})

test('非 Pd 横线 U+02D7、U+2043、U+30FC（及半角 U+FF70）折成 -', () => {
  for (const d of ['˗', '⁃', 'ー', 'ｰ']) {
    const label = d.codePointAt(0)!.toString(16)
    expect([label, escapeUserMarkers(`[auto${d}comic]`)]).toEqual([label, '［auto-comic］'])
    expect([label, escapeUserMarkers(`❲party${d}ambiguous❳`)]).toEqual([label, '［party-ambiguous］'])
  }
})

test('审计绕过样例的转义产物再转一次不变；圆括号、花括号、书名号、直角引号不折', () => {
  const once = escapeUserMarkers('[р\u0430rty] ⟬autoーcomic⟭ ⸢\u0430uto-photo⸣')
  expect(once).toBe('［party］ ［auto-comic］ ［auto-photo］')
  expect(escapeUserMarkers(once)).toBe(once)
  const plain = '(party) （party） {party} ｛party｝ 《party》 「party」 『auto-comic』 ｢party｣ ﹁party﹂ ﹙party﹚ '
    + 'Привет мир ΑΒΓ αβγ ❲注❳ ⟬x⟭ ⦗y⦘ ⸢z⸣ ラーメン ˗ ⁃ [р\u0430rtyx] [\u0430uto comic]'
  expect(escapeUserMarkers(plain)).toBe(plain)
})
