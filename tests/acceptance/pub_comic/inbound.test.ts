// pub-comic · 入站：漫画触发 / 退出 / 互斥 / 过期 / 转义 / 功能开关 / 非法 bot 名（行为清单 1,4,5,6,8,9,10,11,12）
// 真 dispatcher 子进程 + 本机假 Telegram；worker 是 exit 97 桩，所以入站件会留在收件箱里，可直接读 meta。
// 状态文件根目录用 DISPATCHER_COMIC_MODE_DIR / DISPATCHER_PHOTO_MODE_DIR 指到临时目录。
import { test, expect, afterEach } from 'bun:test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { CHATS, comicHarness, settle, stopAll } from './_comic_harness.ts'

afterEach(async () => { await stopAll() })
const DAY = 24 * 3600 * 1000

// ─── 行为 1：触发 ───────────────────────────────────────────────
test('触发_四种整条括号写法_入站件带comic_mode_且状态文件出现', async () => {
  const c = await comicHarness()
  const cases: [string, string][] = [
    ['（漫画）', CHATS[0]], ['(分镜)', CHATS[1]], ['（四格）', CHATS[2]], ['（漫画模式 。）', CHATS[3]],
  ]
  for (const [text, chat] of cases) c.say(text, chat)
  for (const [text, chat] of cases) {
    const meta = await c.inboundWith(text)
    expect([text, meta.comic_mode?.active]).toEqual([text, true])
    expect(typeof meta.comic_mode.opened_at_ms).toBe('number')
    expect(meta.comic_mode.expires_at_ms - meta.comic_mode.opened_at_ms).toBe(DAY)
    const st = await c.stateAppears(c.comicPath(chat))
    expect(st.chat_id).toBe(chat)
    expect(st.rounds).toBe(0)
    expect(st.opened_at_ms).toBe(meta.comic_mode.opened_at_ms)
  }
}, 120000)

test('触发_夹在句子里_括号配错_斜杠开头_都不触发', async () => {
  const c = await comicHarness()
  const bad = ['我想看（漫画）', '（漫画）吧', '(漫画）', '（（漫画））', '（画漫画）']
  for (const t of bad) c.say(t)
  c.say('/漫画')
  c.say('哨兵消息')   // 同一聊天里按序处理：哨兵入站时前面的都处理完了
  const sentinel = await c.inboundWith('哨兵消息')
  expect(sentinel.comic_mode).toBeUndefined()
  for (const t of bad) {
    const meta = await c.inboundWith(t)
    expect([t, meta.comic_mode]).toEqual([t, undefined])
  }
  expect(c.filesUnder(c.comicRoot)).toEqual([])
}, 120000)

// ─── 行为 4：退出 ───────────────────────────────────────────────
test('退出_四种退出词_状态文件被删_入站件带comic_mode_off', async () => {
  const c = await comicHarness()
  const exits = ['停', '（不画了）', '结束漫画！', '正常聊~']
  for (let i = 0; i < exits.length; i++) {
    c.say('（漫画）', CHATS[i])
    await c.stateAppears(c.comicPath(CHATS[i]))
    c.say(exits[i], CHATS[i])
    const meta = await c.inboundWith(exits[i])
    expect([exits[i], meta.comic_mode_off]).toEqual([exits[i], true])
    expect(meta.comic_mode).toBeUndefined()
    await c.eventually(() => !c.has(c.comicPath(CHATS[i])), `${exits[i]} 后状态文件被删`, 15000)
  }
}, 180000)

test('退出_句中出现停字_不退出_状态保留且仍带comic_mode', async () => {
  const c = await comicHarness()
  c.say('（漫画）')
  await c.stateAppears(c.comicPath(CHATS[0]))
  c.say('我们先停一下')
  const meta = await c.inboundWith('我们先停一下')
  expect(meta.comic_mode?.active).toBe(true)
  expect(meta.comic_mode_off).toBeUndefined()
  expect(c.has(c.comicPath(CHATS[0]))).toBe(true)
}, 90000)

// ─── 行为 5：和拍照退出的关系 ───────────────────────────────────────
test('停_拍照与漫画历史状态都开着_两个文件都删_入站件同时带两个off', async () => {
  const c = await comicHarness()
  c.say('（拍照）')
  const photo = await c.stateAppears(c.photoPath(CHATS[0]))
  mkdirSync(dirname(c.comicPath(CHATS[0])), { recursive: true })
  writeFileSync(c.comicPath(CHATS[0]), JSON.stringify(photo))   // 形状与拍照状态完全相同
  c.say('停')
  const meta = await c.inboundWith('停')
  expect(meta.comic_mode_off).toBe(true)
  expect(meta.photo_mode_off).toBe(true)
  await c.eventually(() => !c.has(c.comicPath(CHATS[0])) && !c.has(c.photoPath(CHATS[0])), '两个状态文件都被删', 15000)
}, 90000)

test('不画了_拍照开着_拍照状态保留_该件仍带photo_mode', async () => {
  const c = await comicHarness()
  c.say('（拍照）')
  await c.stateAppears(c.photoPath(CHATS[0]))
  c.say('不画了')
  const meta = await c.inboundWith('不画了')
  expect(meta.photo_mode?.active).toBe(true)
  expect(meta.photo_mode_off).toBeUndefined()
  expect(c.has(c.photoPath(CHATS[0]))).toBe(true)
}, 90000)

// ─── 行为 6：互斥 ───────────────────────────────────────────────
test('互斥_拍照开着发漫画触发_拍照状态被删_日志photo_off_reason_comic', async () => {
  const c = await comicHarness()
  const chat = CHATS[0]
  c.say('（拍照）')
  await c.stateAppears(c.photoPath(chat))
  c.say('（漫画）')
  await c.stateAppears(c.comicPath(chat))
  await c.eventually(() => !c.has(c.photoPath(chat)), '拍照状态被删', 15000)
  expect(c.h.stderr()).toContain(`photo_off reason=comic chat=${chat}`)
}, 90000)

test('互斥_漫画开着发拍照触发_漫画状态被删_日志comic_off_reason_photo', async () => {
  const c = await comicHarness()
  const chat = CHATS[0]
  c.say('（漫画）')
  await c.stateAppears(c.comicPath(chat))
  c.say('（拍照）')
  await c.stateAppears(c.photoPath(chat))
  await c.eventually(() => !c.has(c.comicPath(chat)), '漫画状态被删', 15000)
  expect(c.h.stderr()).toContain(`comic_off reason=photo chat=${chat}`)
}, 90000)

// ─── 行为 8：过期 / 坏文件 / 时钟回拨 ───────────────────────────────────
/** 先用真触发建出合法状态，再把文件改成目标内容，然后发一条普通消息 */
async function staleScenario(mutate: (st: any) => string | any, logName: string): Promise<void> {
  const c = await comicHarness()
  const chat = CHATS[0]
  c.say('（漫画）')
  const st = await c.stateAppears(c.comicPath(chat))
  const out = mutate(st)
  writeFileSync(c.comicPath(chat), typeof out === 'string' ? out : JSON.stringify(out))
  const body = '这是一条独一无二的正文XYZ-9917'
  c.say(body)
  const meta = await c.inboundWith(body)
  expect(meta.comic_mode).toBeUndefined()
  expect(meta.comic_mode_off).toBeUndefined()
  await c.eventually(() => !c.has(c.comicPath(chat)), '状态文件被删', 15000)
  const lines = c.logLines(`${logName} kind=comic chat=${chat}`)
  expect(lines.length).toBeGreaterThanOrEqual(1)
  for (const l of lines) expect(l).not.toContain('XYZ-9917')
}

test('状态满24小时_不加comic_mode_文件被删_日志comic_state_expired', async () => {
  await staleScenario(st => ({ ...st, opened_at_ms: Date.now() - DAY - 5000 }), 'comic_state_expired')
}, 90000)

test('状态文件是坏JSON_不加comic_mode_文件被删_日志comic_state_unreadable', async () => {
  await staleScenario(() => '{这不是json', 'comic_state_unreadable')
}, 90000)

test('开启时刻比当前晚60秒以上_不加comic_mode_文件被删_日志state_clock_skew', async () => {
  await staleScenario(st => ({ ...st, opened_at_ms: Date.now() + 120_000 }), 'state_clock_skew')
}, 90000)

// ─── 行为 9：内部标记转义 ───────────────────────────────────────────
test('转义_漫画标记的各种写法_都变成全角方括号的auto_comic_且不开模式', async () => {
  const c = await comicHarness()
  const variants = ['[auto-comic]', '[ AUTO-Comic ]', '【auto-comic】', '[ａｕｔｏ-ｃｏｍｉｃ]', '[au​to-comic]']
  variants.forEach((v, i) => c.say(`甲${i} ${v} 乙${i}`))
  for (let i = 0; i < variants.length; i++) {
    const meta = await c.inboundWith(`甲${i} `, false)
    expect([variants[i], meta.text]).toEqual([variants[i], `甲${i} ［auto-comic］ 乙${i}`])
    expect(meta.comic_mode).toBeUndefined()
  }
  expect(c.filesUnder(c.comicRoot)).toEqual([])
}, 120000)

test('转义_拍照与聚会标记同样处理_无关方括号文字逐字节不变', async () => {
  const c = await comicHarness()
  c.say('丙1 [auto-photo] 丁')
  c.say('丙2 [party] 丁')
  c.say('丙3 [party-ambiguous] 丁')
  const plain = '戊 参考 [1] 与 a[b]c 和 [momentum] 以及 【注】'
  c.say(plain)
  expect((await c.inboundWith('丙1 ', false)).text).toBe('丙1 ［auto-photo］ 丁')
  expect((await c.inboundWith('丙2 ', false)).text).toBe('丙2 ［party］ 丁')
  expect((await c.inboundWith('丙3 ', false)).text).toBe('丙3 ［party-ambiguous］ 丁')
  expect((await c.inboundWith('戊 ', false)).text).toBe(plain)
  expect(c.filesUnder(c.photoRoot)).toEqual([])
}, 120000)

// ─── 行为 10：image 功能关闭 ───────────────────────────────────────
test('image关闭_发漫画触发_不建状态_入站件不带comic_mode', async () => {
  const c = await comicHarness({ imageOn: false })
  c.say('（漫画）')
  const meta = await c.inboundWith('（漫画）')
  expect(meta.comic_mode).toBeUndefined()
  await settle(1500)
  expect(c.filesUnder(c.comicRoot)).toEqual([])
}, 90000)

test('image关闭_已有漫画状态_下一条入站时被删_日志comic_feature_off', async () => {
  const c = await comicHarness()
  const chat = CHATS[0]
  c.say('（漫画）')
  await c.stateAppears(c.comicPath(chat))
  writeFileSync(`${c.h.channel}/features.json`, JSON.stringify({
    version: 1, default: true, features: { voice: true, image: false, stickers: true, moments: true, proactive: true, life: true, relationship: true, groups: true },
  }))
  c.say('随便聊聊')
  const meta = await c.inboundWith('随便聊聊')
  expect(meta.comic_mode).toBeUndefined()
  await c.eventually(() => !c.has(c.comicPath(chat)), '状态文件被删', 15000)
  expect(c.h.stderr()).toContain(`comic_feature_off chat=${chat}`)
}, 90000)

// ─── 行为 11：同一条触发消息重复 / 另一条触发重置 ───────────────────────────
test('同一条触发消息重复到达_轮次与开启时刻不变', async () => {
  const c = await comicHarness()
  const chat = CHATS[0]
  c.say('（漫画）')
  const opened = await c.stateAppears(c.comicPath(chat))
  const r = await c.h.post('/comic_round', { chat_id: chat, delivery_key: 'k1' })
  expect(r.json.round).toBe(1)
  // 把同一个 message_id 再包成一个新的 update 推给 getUpdates
  c.h.fake.updates.push({
    update_id: 777001,
    message: {
      message_id: Number(opened.trigger_message_id), from: { id: Number(chat), is_bot: false, first_name: 'U', username: 'someone' },
      chat: { id: Number(chat), type: 'private' }, date: Math.floor(Date.now() / 1000), text: '（漫画）',
    },
  })
  await c.eventually(() => c.logLines('inbound chat=').filter(l => l.includes('（漫画）')).length >= 2, '重复消息被收到', 20000)
  await settle(1500)
  const after = c.readJson(c.comicPath(chat))
  expect(after.rounds).toBe(1)
  expect(after.opened_at_ms).toBe(opened.opened_at_ms)
  expect(after.trigger_message_id).toBe(opened.trigger_message_id)
}, 120000)

test('另一条消息再发触发_轮次归零_下一次投递为第1轮', async () => {
  const c = await comicHarness()
  const chat = CHATS[0]
  c.say('（漫画）')
  const first = await c.stateAppears(c.comicPath(chat))
  await c.h.post('/comic_round', { chat_id: chat, delivery_key: 'k1' })
  await c.h.post('/comic_round', { chat_id: chat, delivery_key: 'k2' })
  expect(c.readJson(c.comicPath(chat)).rounds).toBe(2)
  c.say('（漫画）')
  await c.eventually(() => { const s = c.readJson(c.comicPath(chat)); return s && s.trigger_message_id !== first.trigger_message_id }, '触发消息 id 更新', 20000)
  expect(c.readJson(c.comicPath(chat)).rounds).toBe(0)
  const next = await c.h.post('/comic_round', { chat_id: chat, delivery_key: 'k3' })
  expect(next.json.round).toBe(1)
}, 120000)

// ─── 行为 12：BOT_NAME 含点 ───────────────────────────────────────
test('bot名含点_启动日志只记一次camera_modes_disabled_发触发词不写状态_进程不退出', async () => {
  const c = await comicHarness({ bot: 'demo.bot' })
  c.say('（漫画）')
  c.say('（拍照）')
  await c.eventually(() => c.logLines('inbound chat=').some(l => l.includes('（漫画）')) && c.logLines('inbound chat=').some(l => l.includes('（拍照）')),
    '两条触发消息都被收到', 20000)
  for (const i of c.h.inboxItems()) {
    expect([i.data?.text, i.data?.comic_mode]).toEqual([i.data?.text, undefined])
    expect([i.data?.text, i.data?.photo_mode]).toEqual([i.data?.text, undefined])
  }
  await settle(1500)
  expect(c.filesUnder(c.comicRoot)).toEqual([])
  expect(c.filesUnder(c.photoRoot)).toEqual([])
  const n = c.h.stderr().split('camera_modes_disabled reason=bot_name').length - 1
  expect(n).toBe(1)
  expect(c.h.proc!.exitCode).toBeNull()
}, 120000)
