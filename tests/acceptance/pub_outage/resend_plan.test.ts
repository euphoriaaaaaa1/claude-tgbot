// pub-outage · resend_plan.ts 公开函数验收（行为清单 #12 #13 #14 #15 #16 #17）
// 只依据 pub-outage-接口.md 6.4a / 6.5。全同步函数，临时目录 + 假 io，不起任何进程。
import { test, expect } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const load = async (): Promise<any> => await import('../../../dispatcher/resend_plan')

const CHAT = '123456789'
const F = new Date(2026, 10, 2, 12, 0, 0).getTime() // 本地正午，避免跨日格式干扰
const HOUR12 = 43_200_000
const isWin = process.platform === 'win32'
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0

const failed = (index: number, kind = 'text', reason = 'link_down'): any => ({ index, kind, outcome: 'failed', reason })
const txt = (t: string): any => ({ text: t })

function tmpDir(): string { return mkdtempSync(join(tmpdir(), 'pubout-rp-')) }
function hhmm(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function apply(rp: any, prev: any, parts: any[], sources: any[], nowMs: number, epoch = 0): any {
  return rp.applySendResult(prev, CHAT, parts, sources, { nowMs, epoch })
}
function tickCtx(nowMs: number, over: Record<string, unknown> = {}): any {
  return { nowMs, epoch: 0, linkUp: true, linkUpSinceMs: F - 1000, blocked: false, inboxPresent: false, ...over }
}

const FIRST_ROUND1 = (t: string) => `[resend-check] 系统提醒：${t} 你在这个私聊里发出的回复中，下列内容因线路故障确定没有送达，对方没有看到（引号内是你此前的原话，仅供参考）：`
const FIRST_ROUND2 = (t: string) => `[resend-check] 系统提醒：${t} 你在上一次补发提醒之后发出的下列内容又没有送达，对方没有看到（引号内是你此前的原话，仅供参考）：`
const LAST_ROUND1 = '线路现已恢复。请结合现在的时间和此后的对话判断这些内容是否仍需让对方知道：仍需要的，按现在的情况用自己的话重新说，不照搬原文，图片仍合适的用原路径重新发送；此后的回复已经说过、或者内容已经不合时宜的，不必再发，本轮不回复即可。不要提及系统提醒本身。'
const LAST_ROUND2 = '线路现已恢复。上一次提醒里列过的内容不再重复列出，请只处理这里列出的：仍需要的，按现在的情况用自己的话重新说，不照搬原文，图片仍合适的用原路径重新发送；已经说过或已不合时宜的，不必再发，本轮不回复即可。不要提及系统提醒本身。'

// ─── #12 ─────────────────────────────────────────────────────────────────────
test('eligible_link_down失败的四种段都入选', async () => {
  const rp = await load()
  expect(rp.eligible(failed(0, 'text'), txt('a'))).toBe(true)
  expect(rp.eligible(failed(0, 'voice'), txt('念稿'))).toBe(true)
  expect(rp.eligible(failed(0, 'photo'), { path: '/m/a.png' })).toBe(true)
  expect(rp.eligible(failed(0, 'document'), { path: '/m/a.pdf' })).toBe(true)
})

test('eligible_network失败只认无fallback的文字段', async () => {
  const rp = await load()
  expect(rp.eligible(failed(0, 'text', 'network'), txt('a'))).toBe(true)
  expect(rp.eligible({ ...failed(0, 'text', 'network'), fallback: 'text' }, txt('a'))).toBe(false)
  expect(rp.eligible(failed(0, 'voice', 'network'), txt('a'))).toBe(false)
  expect(rp.eligible(failed(0, 'photo', 'network'), { path: '/m/a.png' })).toBe(false)
})

test('eligible_非失败或其它原因或来源缺失_一律不入选', async () => {
  const rp = await load()
  for (const outcome of ['sent', 'unknown', 'skipped']) {
    expect([outcome, rp.eligible({ index: 0, kind: 'text', outcome, reason: 'link_down' }, txt('a'))]).toEqual([outcome, false])
  }
  expect(rp.eligible(failed(0, 'text', 'rejected'), txt('a'))).toBe(false)
  expect(rp.eligible(failed(0, 'text'), null)).toBe(false)
  expect(rp.eligible(failed(0, 'text'), txt(''))).toBe(false)
  expect(rp.eligible(failed(0, 'photo'), { path: '' })).toBe(false)
})

// ─── #13 ─────────────────────────────────────────────────────────────────────
test('applySendResult_同内容只计首个_resendRecord把重复段号并回', async () => {
  const rp = await load()
  const parts = [0, 1, 2, 3].map(i => failed(i))
  const sources = ['A', 'B', 'A', 'B'].map(txt)
  const r = apply(rp, null, parts, sources, F)
  expect(r.state.parts.length).toBe(2)
  expect(r.scheduled).toEqual([0, 1])
  expect(r.event.kind).toBe('recorded')
  const dir = tmpDir()
  try {
    expect(rp.resendRecord(dir, CHAT, parts, sources, F)).toEqual([0, 1, 2, 3])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('applySendResult_先状态不明再失败同内容_不入待补_只记摘要', async () => {
  const rp = await load()
  const u = apply(rp, null, [{ index: 0, kind: 'text', outcome: 'unknown' }], [txt('X')], F)
  const key = rp.contentKey('text', 'X')
  expect(Object.keys(u.state.unknown_keys)).toContain(key)
  const f = apply(rp, u.state, [failed(0)], [txt('X')], F + 1000)
  expect(f.scheduled).toEqual([])
  expect(f.state.parts.length).toBe(0)
  expect(Object.keys(f.state.unknown_keys)).toContain(key)
})

test('applySendResult_已待补的内容后来送达_移出待补', async () => {
  const rp = await load()
  const a = apply(rp, null, [failed(0)], [txt('X')], F)
  expect(a.state.parts.length).toBe(1)
  const b = apply(rp, a.state, [{ index: 0, kind: 'text', outcome: 'sent' }], [txt('X')], F + 1000)
  expect(b.event.sent_removed).toBe(1)
  expect((b.state?.parts ?? []).length).toBe(0)
})

test('applySendResult_chatId不是纯数字_原状态返回_不入批', async () => {
  const rp = await load()
  const a = apply(rp, null, [failed(0)], [txt('X')], F)
  for (const bad of ['-100123', 'abc']) {
    const r = rp.applySendResult(a.state, bad, [failed(0)], [txt('Y')], { nowMs: F + 1000, epoch: 0 })
    expect([bad, r.state === a.state, r.scheduled, r.event.kind]).toEqual([bad, true, [], 'bad_chat'])
  }
})

test('applySendResult_非有限数时间_不抛', async () => {
  const rp = await load()
  expect(() => rp.applySendResult(null, CHAT, [failed(0)], [txt('X')], { nowMs: NaN, epoch: 0 })).not.toThrow()
})

test('contentKey_是kind加零字符加内容的sha256前16位十六进制', async () => {
  const rp = await load()
  const expected = new Bun.CryptoHasher('sha256').update('text\u0000X').digest('hex').slice(0, 16)
  expect(rp.contentKey('text', 'X')).toBe(expected)
  expect(rp.contentKey('voice', 'X')).not.toBe(expected)
})

test('withResendParts_只留非负整数_去重升序_为空返回同一对象', async () => {
  const rp = await load()
  expect(rp.withResendParts({ ok: true }, [3, 1, 1, -1, 2.5, 0])).toEqual({ ok: true, resend_parts: [0, 1, 3] })
  const body = { ok: true }
  expect(rp.withResendParts(body, [])).toBe(body)
  expect(rp.withResendParts(body, [-1, 1.5])).toBe(body)
})

test('listResendChats_只列纯数字文件名_clear_epoch缺失或非法为0_bump加1', async () => {
  const rp = await load()
  const dir = tmpDir()
  try {
    mkdirSync(join(dir, '.resend'), { recursive: true })
    for (const n of ['123.json', '456.json', 'abc.json', '-100.json', 'clear-epoch']) writeFileSync(join(dir, '.resend', n), '{}')
    expect(rp.listResendChats(dir).slice().sort()).toEqual(['123', '456'])
    rmSync(join(dir, '.resend', 'clear-epoch'))
    expect(rp.readClearEpoch(dir)).toBe(0)
    writeFileSync(join(dir, '.resend', 'clear-epoch'), 'abc')
    expect(rp.readClearEpoch(dir)).toBe(0)
    rp.bumpClearEpoch(dir)
    expect(rp.readClearEpoch(dir)).toBe(1)
    rp.bumpClearEpoch(dir)
    expect(rp.readClearEpoch(dir)).toBe(2)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

// ─── #14 ─────────────────────────────────────────────────────────────────────
function twoPending(rp: any): any {
  return apply(rp, null, [failed(0), failed(1)], [txt('甲'), txt('乙')], F).state
}

test('decideResendTick_恢复满120秒才投件_差1秒不投_投件后待补清空', async () => {
  const rp = await load()
  const s0 = twoPending(rp)
  expect(rp.decideResendTick(s0, tickCtx(F + 119_000)).action).toBe('none')
  const d = rp.decideResendTick(s0, tickCtx(F + 121_000))
  expect(d.action).toBe('inject')
  expect(d.inject.round).toBe(1)
  expect(d.inject.parts.length).toBe(2)
  expect(d.state.parts.length).toBe(0)
  // 恢复起点晚于最后失败：从恢复起点起算
  const late = tickCtx(F + 121_000, { linkUpSinceMs: F + 100_000 })
  expect(rp.decideResendTick(s0, late).action).toBe('none')
  expect(rp.decideResendTick(s0, tickCtx(F + 221_000, { linkUpSinceMs: F + 100_000 })).action).toBe('inject')
})

test('decideResendTick_被拦或线路不通_不投件', async () => {
  const rp = await load()
  const s0 = twoPending(rp)
  expect(rp.decideResendTick(s0, tickCtx(F + 121_000, { blocked: true })).action).toBe('none')
  expect(rp.decideResendTick(s0, tickCtx(F + 121_000, { linkUp: false, linkUpSinceMs: null })).action).toBe('none')
})

test('decideResendTick_件还在收件箱里_不叠投', async () => {
  const rp = await load()
  const s1 = rp.decideResendTick(twoPending(rp), tickCtx(F + 121_000)).state
  const s2 = apply(rp, s1, [failed(0)], [txt('Y')], F + 130_000).state
  const d = rp.decideResendTick(s2, tickCtx(F + 400_000, { inboxPresent: true }))
  expect(d.action).toBe('none')
})

/** 走到某一轮投件之后：投件、取走、再来一次新失败。返回新状态与时间。 */
function roundTrip(rp: any, state: any, injectAt: number, nextFailText: string): { state: any; round: number; inject: any; failAt: number } {
  const d = rp.decideResendTick(state, tickCtx(injectAt))
  expect(d.action).toBe('inject')
  const taken = rp.decideResendTick(d.state, tickCtx(injectAt + 5_000, { inboxPresent: false }))
  expect(taken.action).toBe('none')
  const failAt = injectAt + 60_000
  const s = apply(rp, taken.state, [failed(0)], [txt(nextFailText)], failAt).state
  return { state: s, round: d.inject.round, inject: d.inject, failAt }
}

test('decideResendTick_第一轮取走后又失败_第二轮只列新失败的段', async () => {
  const rp = await load()
  const t1 = F + 121_000
  const r1 = roundTrip(rp, twoPending(rp), t1, 'Y')
  expect(r1.round).toBe(1)
  const d2 = rp.decideResendTick(r1.state, tickCtx(r1.failAt + 121_000))
  expect(d2.action).toBe('inject')
  expect(d2.inject.round).toBe(2)
  expect(d2.inject.parts.length).toBe(1)
  expect(d2.inject.parts[0].text).toBe('Y')
})

test('decideResendTick_取走后15分钟没有新失败_done_状态不明摘要保留', async () => {
  const rp = await load()
  const withUnknown = apply(rp, null, [{ index: 0, kind: 'text', outcome: 'unknown' }, failed(1)], [txt('U'), txt('A')], F).state
  const t1 = F + 121_000
  const d1 = rp.decideResendTick(withUnknown, tickCtx(t1))
  expect(d1.action).toBe('inject')
  const taken = rp.decideResendTick(d1.state, tickCtx(t1 + 5_000))
  expect(taken.action).toBe('none')
  const early = rp.decideResendTick(taken.state, tickCtx(t1 + 5_000 + 840_000))
  expect(early.action).toBe('none')
  const done = rp.decideResendTick(taken.state, tickCtx(t1 + 5_000 + 901_000))
  expect(done.action).toBe('done')
  expect(Object.keys(done.state.unknown_keys)).toContain(rp.contentKey('text', 'U'))
})

/** 三轮都用完且第三轮取走后仍有新失败 -> give_up */
function driveToGiveUp(rp: any): { state: any; at: number; epoch: number } {
  let state = twoPending(rp)
  let injectAt = F + 121_000
  let last: any
  for (let i = 0; i < 3; i++) {
    last = roundTrip(rp, state, injectAt, `新失败${i}`)
    state = last.state
    injectAt = last.failAt + 121_000
  }
  expect(last.round).toBe(3)
  let at = last.failAt + 1_000
  for (let k = 0; k < 4; k++) {
    const d = rp.decideResendTick(state, tickCtx(at))
    if (d.action === 'give_up') return { state: d.state, at, epoch: 0 }
    expect(d.action).toBe('none')
    state = d.state
    at += 1_000
  }
  throw new Error('第三轮取走后仍有待补，应在几个 tick 内 give_up，但没有')
}

test('decideResendTick_三轮用完仍有新失败_give_up_冷却12小时', async () => {
  const rp = await load()
  const g = driveToGiveUp(rp)
  expect(g.state.cooldown_until_ms).toBe(g.at + HOUR12)
})

test('冷却中_新失败不入批_出现一次sent后可开新批', async () => {
  const rp = await load()
  const g = driveToGiveUp(rp)
  const cooled = apply(rp, g.state, [failed(0)], [txt('Q')], g.at + 1000)
  expect(cooled.scheduled).toEqual([])
  expect(cooled.event.kind).toBe('cooldown')
  const afterSent = apply(rp, g.state, [{ index: 0, kind: 'text', outcome: 'sent' }], [txt('OK')], g.at + 2000)
  const reopened = apply(rp, afterSent.state, [failed(0)], [txt('Q')], g.at + 3000)
  expect(reopened.scheduled).toEqual([0])
})

test('冷却中_清空会话代数变化后可开新批', async () => {
  const rp = await load()
  const g = driveToGiveUp(rp)
  const dir = tmpDir()
  try {
    const before = rp.readClearEpoch(dir)
    rp.bumpClearEpoch(dir)
    const after = rp.readClearEpoch(dir)
    expect(after).toBe(before + 1)
    const reopened = apply(rp, g.state, [failed(0)], [txt('Q')], g.at + 1000, after)
    expect(reopened.scheduled).toEqual([0])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('decideResendTick_会话代数变化_void_件还在则给出撤回键', async () => {
  const rp = await load()
  const s0 = twoPending(rp)
  expect(rp.decideResendTick(s0, tickCtx(F + 10_000, { epoch: 1 })).action).toBe('void')
  const d1 = rp.decideResendTick(s0, tickCtx(F + 121_000))
  const v = rp.decideResendTick(d1.state, tickCtx(F + 130_000, { epoch: 1, inboxPresent: true }))
  expect(v.action).toBe('void')
  expect(v.removeInboxKey).toBe(d1.inject.inboxKey)
})

test('decideResendTick_同内容再次失败_首次时刻保留_等待从最近一次起算_提醒用首次时刻', async () => {
  const rp = await load()
  const s1 = apply(rp, null, [failed(0)], [txt('R')], F).state
  const s2 = apply(rp, s1, [failed(0)], [txt('R')], F + 600_000).state
  expect(s2.parts.length).toBe(1)
  expect(s2.parts[0].at_ms).toBe(F + 600_000)
  expect(s2.parts[0].first_ms).toBe(F)
  const ctx = (now: number) => tickCtx(now, { linkUpSinceMs: F - 1000 })
  expect(rp.decideResendTick(s2, ctx(F + 600_000 + 119_000)).action).toBe('none')
  const d = rp.decideResendTick(s2, ctx(F + 600_000 + 121_000))
  expect(d.action).toBe('inject')
  const text = rp.resendMessageText(d.inject.parts, 1, F + 600_000 + 121_000, () => true, d.inject.omitted ?? 0)
  expect(text.split('\n')[0]).toBe(FIRST_ROUND1(hhmm(F)))
})

// ─── #15 ─────────────────────────────────────────────────────────────────────
const pp = (kind: string, content: string, atMs = F, extra: Record<string, unknown> = {}): any => ({
  key: '0123456789abcdef', kind, ...(kind === 'photo' || kind === 'document' ? { path: content } : { text: content }), reason: 'link_down', at_ms: atMs, ...extra,
})

test('resendMessageText_第1轮与第2轮正文逐字相同', async () => {
  const rp = await load()
  const now = F + 130_000
  const r1 = rp.resendMessageText([pp('text', '你好')], 1, now, () => true)
  expect(r1).toBe([FIRST_ROUND1(hhmm(F)), '1. 文字：「你好」', LAST_ROUND1].join('\n'))
  const r2 = rp.resendMessageText([pp('text', '你好')], 2, now, () => true)
  expect(r2).toBe([FIRST_ROUND2(hhmm(F)), '1. 文字：「你好」', LAST_ROUND2].join('\n'))
  const r3 = rp.resendMessageText([pp('text', '你好')], 3, now, () => true)
  expect(r3.split('\n')[0]).toBe(FIRST_ROUND2(hhmm(F)))
})

test('resendMessageText_原话里的书名号换成双书名号_换行换成空格_语音行写念稿', async () => {
  const rp = await load()
  const out = rp.resendMessageText([pp('text', '他说「好」\n再见'), pp('voice', '晚安\r\n好梦', F + 1000)], 1, F + 130_000, () => true)
  const lines = out.split('\n')
  expect(lines[1]).toBe('1. 文字：「他说『好』 再见」')
  expect(lines[2]).toBe('2. 语音（念稿）：「晚安 好梦」')
})

test('resendMessageText_图片和文件行_路径存在写路径_不存在写已不存在', async () => {
  const rp = await load()
  const exists = (p: string) => p === '/m/ok.png' || p === '/m/ok.pdf'
  const parts = [pp('photo', '/m/ok.png', F), pp('photo', '/m/gone.png', F + 1000), pp('document', '/m/ok.pdf', F + 2000), pp('document', '/m/gone.pdf', F + 3000)]
  const lines = rp.resendMessageText(parts, 1, F + 130_000, exists).split('\n')
  expect(lines.slice(1, 5)).toEqual([
    '1. 图片：/m/ok.png',
    '2. 图片：原文件已不存在，无法重发。',
    '3. 文件：/m/ok.pdf',
    '4. 文件：原文件已不存在，无法重发。',
  ])
})

test('resendMessageText_各段首次失败跨度超10分钟_首行写最早至最晚', async () => {
  const rp = await load()
  const out = rp.resendMessageText([pp('text', 'a', F), pp('text', 'b', F + 11 * 60_000)], 1, F + 20 * 60_000, () => true)
  expect(out.split('\n')[0]).toBe(FIRST_ROUND1(`${hhmm(F)} 至 ${hhmm(F + 11 * 60_000)}`))
})

test('待补25段投件_只列最早20段_末行写另有5段_25段都不再待补', async () => {
  const rp = await load()
  const n = 25
  const sources = Array.from({ length: n }, (_, i) => txt(`m${String(i).padStart(2, '0')}`))
  // 各段失败时间错开 1 秒，最早的是 m00
  let s: any = null
  for (let i = 0; i < n; i++) s = apply(rp, s, [failed(0)], [sources[i]], F + i * 1000).state
  expect(s.parts.length).toBe(n)
  const d = rp.decideResendTick(s, tickCtx(F + n * 1000 + 121_000))
  expect(d.action).toBe('inject')
  expect(d.state.parts.length).toBe(0)
  expect(d.inject.omitted).toBe(5)
  const out = rp.resendMessageText(d.inject.parts, d.inject.round, F + 200_000, () => true, d.inject.omitted)
  const lines = out.split('\n')
  const items = lines.filter((l: string) => /^\d+\. /.test(l))
  expect(items.length).toBe(20)
  expect(items[0]).toContain('m00')
  expect(items[19]).toContain('m19')
  expect(out).not.toContain('m20')
  expect(lines[lines.length - 1]).toBe('另有 5 段内容因篇幅未列出。')
})

test('resendMessageText_整条超2500字_从最后一段往前把原话缩成前60字加后略', async () => {
  const rp = await load()
  const long = (c: string) => c.repeat(400)
  const parts = Array.from({ length: 15 }, (_, i) => pp('text', long(String.fromCharCode(97 + i)), F + i * 1000))
  const out = rp.resendMessageText(parts, 1, F + 200_000, () => true)
  expect(out.length).toBeLessThanOrEqual(2500)
  const items = out.split('\n').filter((l: string) => /^\d+\. /.test(l))
  expect(items.length).toBe(15)
  expect(items[0]).toBe(`1. 文字：「${'a'.repeat(400)}」`)               // 靠前的段保持原样
  expect(items[14]).toBe(`15. 文字：「${'o'.repeat(60)}（后略）」`)         // 最后一段被缩到前 60 字
})

test('记录待补时_单段文字只存前500字_超出带后略标记', async () => {
  const rp = await load()
  const r = apply(rp, null, [failed(0)], [txt('字'.repeat(600))], F)
  const saved: string = r.state.parts[0].text
  expect(saved.endsWith('（后略）')).toBe(true)
  expect(saved.startsWith('字'.repeat(490))).toBe(true)
  expect(saved.length).toBeGreaterThanOrEqual(495)
  expect(saved.length).toBeLessThanOrEqual(505)
})

// ─── #16 ─────────────────────────────────────────────────────────────────────
test('补发全流程_记4段_重发2段_满120秒投件一次_取走_15分钟后done_摘要保留', async () => {
  const rp = await load()
  const dir = tmpDir()
  try {
    const parts4 = [0, 1, 2, 3].map(i => failed(i))
    const names = ['A', 'B', 'C', 'D']
    const rec = rp.resendRecord(dir, CHAT, parts4, names.map(txt), F)
    expect(rec).toEqual([0, 1, 2, 3])
    expect(rp.readResendState(dir, CHAT).parts.length).toBe(4)
    expect(rec instanceof Promise).toBe(false)

    // 一段状态不明（只留摘要）
    rp.resendRecord(dir, CHAT, [{ index: 0, kind: 'text', outcome: 'unknown' }], [txt('U')], F + 500)
    // worker 原样重发了 A、B
    rp.resendRecord(dir, CHAT, [{ index: 0, kind: 'text', outcome: 'sent' }, { index: 1, kind: 'text', outcome: 'sent' }], [txt('A'), txt('B')], F + 1000)
    expect(rp.readResendState(dir, CHAT).parts.length).toBe(2)

    const inboxKeys = new Set<string>()
    const writes: { chatId: string; key: string; meta: any }[] = []
    const logs: string[] = []
    const io = {
      inboxPresent: (key: string, _chatId: string) => inboxKeys.has(key),
      writeInbox: (chatId: string, key: string, meta: any) => { writes.push({ chatId, key, meta }); inboxKeys.add(key) },
      removeInbox: (_chatId: string, key: string) => { inboxKeys.delete(key) },
      ensureWorker: (_chatId: string) => {},
      log: (line: string) => { logs.push(line) },
    }
    const ctx = (nowMs: number) => ({ nowMs, linkUp: true, linkUpSinceMs: F - 1000, blocked: false, fileExists: () => true })

    const ret = rp.resendTickAll(dir, ctx(F + 122_000), io)
    expect(ret === undefined || typeof (ret as any)?.then !== 'function').toBe(true)
    expect(writes.length).toBe(1)
    const st = rp.readResendState(dir, CHAT)
    const w = writes[0]
    expect(w.chatId).toBe(CHAT)
    expect(w.key).toBe(`resend${st.batch_ms}r1`)
    expect(w.meta).toEqual({
      text: w.meta.text, chat_id: CHAT, scene: 'private', from_id: CHAT, chat_type: 'private', is_bot_sender: false,
      from_username: 'user', sender_username: 'user', ts: w.meta.ts, message_id: `${st.batch_ms}1`, synthetic: true,
    })
    expect(w.meta.text.startsWith('[resend-check]')).toBe(true)
    expect(w.meta.text).toContain('1. 文字：「C」')
    expect(w.meta.text).toContain('2. 文字：「D」')
    expect(w.meta.text).not.toContain('「A」')
    expect(Number.isNaN(Date.parse(w.meta.ts))).toBe(false)
    expect(w.meta.synthetic).toBe(true)
    expect(logs.some(l => l.includes(`resend_inject chat=${CHAT} round=1/3 parts=2`))).toBe(true)

    // 件还在：不会再投
    rp.resendTickAll(dir, ctx(F + 130_000), io)
    expect(writes.length).toBe(1)
    // 件被取走，再过 15 分钟没有新失败
    inboxKeys.clear()
    rp.resendTickAll(dir, ctx(F + 140_000), io)
    rp.resendTickAll(dir, ctx(F + 140_000 + 901_000), io)
    expect(writes.length).toBe(1)
    expect(logs.some(l => l.includes('resend_done'))).toBe(true)
    const left = rp.readResendState(dir, CHAT)
    expect(left.parts.length).toBe(0)
    expect(Object.keys(left.unknown_keys)).toContain(rp.contentKey('text', 'U'))
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

// ─── #17 ─────────────────────────────────────────────────────────────────────
test('writeResendState_文件0600_目录0700_写null删文件', async () => {
  if (isWin) return
  const rp = await load()
  const dir = tmpDir()
  try {
    const state = apply(rp, null, [failed(0)], [txt('X')], F).state
    expect(rp.writeResendState(dir, CHAT, state)).toBe(true)
    expect(statSync(join(dir, '.resend', `${CHAT}.json`)).mode & 0o777).toBe(0o600)
    expect(statSync(join(dir, '.resend')).mode & 0o777).toBe(0o700)
    expect(rp.writeResendState(dir, CHAT, null)).toBe(true)
    expect(existsSync(join(dir, '.resend', `${CHAT}.json`))).toBe(false)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('readResendState_畸形或版本不对_按无记录_resendRecord覆盖写新状态', async () => {
  const rp = await load()
  const dir = tmpDir()
  try {
    mkdirSync(join(dir, '.resend'), { recursive: true })
    const p = join(dir, '.resend', `${CHAT}.json`)
    writeFileSync(p, '{broken json')
    expect(rp.readResendState(dir, CHAT)).toBeNull()
    writeFileSync(p, JSON.stringify({ v: 1, chat_id: CHAT, parts: [] }))
    expect(rp.readResendState(dir, CHAT)).toBeNull()
    expect(rp.readResendState(dir, '999')).toBeNull() // 不存在
    expect(rp.resendRecord(dir, CHAT, [failed(0)], [txt('X')], F)).toEqual([0])
    const after = JSON.parse(readFileSync(p, 'utf8'))
    expect(after.v).toBe(2)
    expect(after.parts.length).toBe(1)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('resendRecord_状态文件读不了_抛出_原文件字节和权限都不变', async () => {
  if (isWin || isRoot) return
  const rp = await load()
  const dir = tmpDir()
  const p = join(dir, '.resend', `${CHAT}.json`)
  try {
    const state = apply(rp, null, [failed(0)], [txt('旧')], F).state
    expect(rp.writeResendState(dir, CHAT, state)).toBe(true)
    const bytes = readFileSync(p)
    chmodSync(p, 0o000)
    expect(() => rp.resendRecord(dir, CHAT, [failed(0)], [txt('新')], F + 1000)).toThrow()
    expect(statSync(p).mode & 0o777).toBe(0o000)
    chmodSync(p, 0o600)
    expect(readFileSync(p).equals(bytes)).toBe(true)
  } finally {
    try { chmodSync(p, 0o600) } catch { /* ignore */ }
    rmSync(dir, { recursive: true, force: true })
  }
})

test('resendTickAll_写收件箱抛错且回滚也写不了_记reason等于rollback', async () => {
  if (isWin || isRoot) return
  const rp = await load()
  const dir = tmpDir()
  const resendDir = join(dir, '.resend')
  try {
    rp.resendRecord(dir, CHAT, [failed(0)], [txt('X')], F)
    const logs: string[] = []
    const io = {
      inboxPresent: () => false,
      writeInbox: () => { chmodSync(resendDir, 0o500); throw new Error('boom') }, // 抛错的同时让回滚写盘必败
      removeInbox: () => {},
      ensureWorker: () => {},
      log: (l: string) => { logs.push(l) },
    }
    rp.resendTickAll(dir, { nowMs: F + 122_000, linkUp: true, linkUpSinceMs: F - 1000, blocked: false, fileExists: () => true }, io)
    expect(logs.some(l => l.includes('resend_inject_failed reason=rollback'))).toBe(true)
    expect(logs.some(l => /resend_inject_failed reason=Error/.test(l))).toBe(true)
    expect(logs.join('\n')).not.toContain('boom') // 日志只记错误名，不记 message
  } finally {
    try { chmodSync(resendDir, 0o700) } catch { /* ignore */ }
    rmSync(dir, { recursive: true, force: true })
  }
})

test('resendTickAll_单个私聊出错_不影响其它私聊', async () => {
  if (isWin || isRoot) return
  const rp = await load()
  const dir = tmpDir()
  const bad = '111', good = '222'
  try {
    rp.resendRecord(dir, bad, [failed(0)], [txt('X')], F)
    rp.resendRecord(dir, good, [failed(0)], [txt('Y')], F)
    chmodSync(join(dir, '.resend', `${bad}.json`), 0o000)
    const writes: string[] = []
    const logs: string[] = []
    const io = {
      inboxPresent: () => false,
      writeInbox: (chatId: string) => { writes.push(chatId) },
      removeInbox: () => {},
      ensureWorker: () => {},
      log: (l: string) => { logs.push(l) },
    }
    rp.resendTickAll(dir, { nowMs: F + 122_000, linkUp: true, linkUpSinceMs: F - 1000, blocked: false, fileExists: () => true }, io)
    expect(writes).toEqual([good])
    expect(logs.some(l => /resend_tick_failed reason=\w+ chat=111/.test(l))).toBe(true)
  } finally {
    try { chmodSync(join(dir, '.resend', `${bad}.json`), 0o600) } catch { /* ignore */ }
    rmSync(dir, { recursive: true, force: true })
  }
})
