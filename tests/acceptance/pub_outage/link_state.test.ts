// pub-outage · link_state.ts / send_error.ts 公开函数验收（行为清单 #1 #2 #3）
// 只依据 pub-outage-接口.md 6.4a / 6.5。动态 import：模块不存在时每条用例各自红，而不是整个文件一起挂。
import { test, expect } from 'bun:test'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const loadLinkState = async (): Promise<any> => await import('../../../dispatcher/link_state')
const loadSendError = async (): Promise<any> => await import('../../../dispatcher/send_error')

function tmp(): string { return mkdtempSync(join(tmpdir(), 'pubout-ls-')) }

test('线路监视器_常量取值', async () => {
  const m = await loadLinkState()
  expect(m.LINK_DOWN_AFTER_MS).toBe(90_000)
  expect(m.LINK_REPROBE_MS).toBe(60_000)
  expect(m.LINK_STATE_STALE_SEC).toBe(120)
})

// ─── #1 ──────────────────────────────────────────────────────────────────────
test('线路监视器_刚创建不记任何事_不探测', async () => {
  const { createLinkMonitor } = await loadLinkState()
  const mon = createLinkMonitor()
  const t0 = Date.now()
  expect(mon.shouldProbe(t0 + 200_000)).toBe(false)
  expect(mon.isDown()).toBe(false)
  expect(mon.downSinceMs()).toBeNull()
})

test('线路监视器_仅拉取失败_超过90秒无回包才探测', async () => {
  const { createLinkMonitor } = await loadLinkState()
  const mon = createLinkMonitor()
  const t0 = Date.now()
  mon.recordNetworkFailure(t0 + 10_000, 'poll')
  expect(mon.shouldProbe(t0 + 50_000)).toBe(false)
  expect(mon.shouldProbe(t0 + 91_000)).toBe(true)
  expect(mon.isDown()).toBe(false)
})

test('线路监视器_发送失败_立即登记一次探测_探测开始后不再登记', async () => {
  const { createLinkMonitor } = await loadLinkState()
  const mon = createLinkMonitor()
  const t0 = Date.now()
  mon.recordNetworkFailure(t0 + 5_000, 'send')
  expect(mon.shouldProbe(t0 + 5_000)).toBe(true)
  mon.probeStarted(t0 + 5_000)
  expect(mon.shouldProbe(t0 + 5_000)).toBe(false)
  expect(mon.isDown()).toBe(false)
})

test('线路监视器_拉取失败不论多久_isDown永远不会因此变true', async () => {
  const { createLinkMonitor } = await loadLinkState()
  const mon = createLinkMonitor()
  const t0 = Date.now()
  for (let s = 30; s <= 300; s += 30) {
    mon.recordNetworkFailure(t0 + s * 1000, 'poll')
    expect(mon.isDown()).toBe(false)
  }
  mon.recordNetworkFailure(t0 + 310_000, 'send')
  mon.recordNetworkFailure(t0 + 320_000, 'probe')
  expect(mon.isDown()).toBe(false)
  expect(mon.downSinceMs()).toBeNull()
})

test('线路监视器_探测失败进入不通_重探间隔60秒_二次失败不改起点_回包后恢复', async () => {
  const { createLinkMonitor } = await loadLinkState()
  const mon = createLinkMonitor()
  const t0 = Date.now()
  mon.probeStarted(t0 + 100_000)
  mon.probeFinished(t0 + 110_000, false)
  expect(mon.isDown()).toBe(true)
  expect(mon.downSinceMs()).toBe(t0 + 110_000)
  expect(mon.shouldProbe(t0 + 169_000)).toBe(false)
  expect(mon.shouldProbe(t0 + 170_000)).toBe(true)
  mon.probeStarted(t0 + 170_000)
  expect(mon.shouldProbe(t0 + 175_000)).toBe(false) // 探测在飞
  mon.probeFinished(t0 + 180_000, false)
  expect(mon.isDown()).toBe(true)
  expect(mon.downSinceMs()).toBe(t0 + 110_000)
  mon.recordResponse(t0 + 200_000)
  expect(mon.isDown()).toBe(false)
  expect(mon.downSinceMs()).toBeNull()
})

test('线路监视器_自定义阈值生效', async () => {
  const { createLinkMonitor } = await loadLinkState()
  const mon = createLinkMonitor({ downAfterMs: 10_000, reprobeMs: 5_000 })
  const t0 = Date.now()
  mon.recordNetworkFailure(t0 + 1_000, 'poll')
  expect(mon.shouldProbe(t0 + 9_000)).toBe(false)
  expect(mon.shouldProbe(t0 + 11_000)).toBe(true)
  mon.probeStarted(t0 + 11_000)
  mon.probeFinished(t0 + 12_000, false)
  expect(mon.shouldProbe(t0 + 16_000)).toBe(false)
  expect(mon.shouldProbe(t0 + 17_000)).toBe(true)
})

// ─── #2 ──────────────────────────────────────────────────────────────────────
test('线路监视器_startUnknown创建时状态未知_应探测', async () => {
  const { createLinkMonitor } = await loadLinkState()
  const mon = createLinkMonitor({ startUnknown: true })
  const t0 = Date.now()
  expect(mon.isKnown()).toBe(false)
  expect(mon.isDown()).toBe(false)
  expect(mon.shouldProbe(t0)).toBe(true)
})

test('线路监视器_startUnknown_三种结局后都变已知_只有探测失败才不通', async () => {
  const { createLinkMonitor } = await loadLinkState()
  const t0 = Date.now()

  const a = createLinkMonitor({ startUnknown: true })
  a.probeFinished(t0 + 500, true)
  expect(a.isKnown()).toBe(true)
  expect(a.isDown()).toBe(false)

  const b = createLinkMonitor({ startUnknown: true })
  b.probeFinished(t0 + 10_000, false)
  expect(b.isKnown()).toBe(true)
  expect(b.isDown()).toBe(true)
  expect(b.downSinceMs()).toBe(t0 + 10_000)

  const c = createLinkMonitor({ startUnknown: true })
  c.recordResponse(t0 + 100)
  expect(c.isKnown()).toBe(true)
  expect(c.isDown()).toBe(false)
})

// ─── #3 ──────────────────────────────────────────────────────────────────────
test('shouldBlockSend_线路不通时_发送类拦截_收取类放行', async () => {
  const { shouldBlockSend } = await loadLinkState()
  const blocked = ['sendMessage', 'sendPhoto', 'sendDocument', 'sendChatAction', 'editMessageText', 'editMessageCaption',
    'editMessageMedia', 'forwardMessage', 'copyMessage']
  for (const m of blocked) expect([m, shouldBlockSend(m, true)]).toEqual([m, true])
  for (const m of ['getUpdates', 'getMe', 'getFile', 'setMessageReaction']) expect([m, shouldBlockSend(m, true)]).toEqual([m, false])
})

test('shouldBlockSend_线路正常时_不拦sendMessage', async () => {
  const { shouldBlockSend } = await loadLinkState()
  expect(shouldBlockSend('sendMessage', false)).toBe(false)
  expect(shouldBlockSend('sendChatAction', false)).toBe(false)
})

test('LinkDownError_名字与缺省文案_且classifySendError归为link_down', async () => {
  const { LinkDownError } = await loadLinkState()
  const { classifySendError } = await loadSendError()
  const e = new LinkDownError()
  expect(e).toBeInstanceOf(Error)
  expect(e.name).toBe('LinkDownError')
  expect(e.message).toBe('link_down')
  expect(classifySendError(e)).toMatchObject({ cls: 'link_down', reason: 'link_down' })
})

test('writeLinkState_down为true_建目录写恰好一行_且不留tmp文件', async () => {
  const { writeLinkState } = await loadLinkState()
  const base = tmp()
  try {
    const dir = join(base, 'nested', 'ls')
    const now = 1_800_000_000
    writeLinkState(dir, 'b', true, now - 30, now)
    expect(readFileSync(join(dir, 'b'), 'utf8')).toBe(`down_since=${now - 30} checked_at=${now}\n`)
    expect(readdirSync(dir)).toEqual(['b'])
  } finally { rmSync(base, { recursive: true, force: true }) }
})

test('linkPauseReason_窗口内为link_down_超出正负120秒为null_边界含等于', async () => {
  const { writeLinkState, linkPauseReason } = await loadLinkState()
  const base = tmp()
  try {
    const now = 1_800_000_000
    writeLinkState(base, 'b', true, now - 30, now)
    expect(linkPauseReason(base, 'b', now + 30)).toBe('link_down')
    expect(linkPauseReason(base, 'b', now + 120)).toBe('link_down')
    expect(linkPauseReason(base, 'b', now - 120)).toBe('link_down')
    expect(linkPauseReason(base, 'b', now + 121)).toBeNull()
    expect(linkPauseReason(base, 'b', now - 121)).toBeNull()
  } finally { rmSync(base, { recursive: true, force: true }) }
})

test('linkPauseReason_文件缺失_乱码_非整数_超18位_一律视为通', async () => {
  const { linkPauseReason } = await loadLinkState()
  const base = tmp()
  try {
    expect(linkPauseReason(base, 'nofile', 1_800_000_000)).toBeNull()
    writeFileSync(join(base, 'junk'), '\u0000\u0001乱码 not a state file')
    expect(linkPauseReason(base, 'junk', 1_800_000_000)).toBeNull()
    writeFileSync(join(base, 'alpha'), 'down_since=1 checked_at=abc\n')
    expect(linkPauseReason(base, 'alpha', 1_800_000_000)).toBeNull()
    writeFileSync(join(base, 'long'), 'down_since=1 checked_at=1234567890123456789\n') // 19 位
    expect(linkPauseReason(base, 'long', 123456789012345678)).toBeNull()
    writeFileSync(join(base, 'dec'), 'down_since=1 checked_at=1800000000.5\n')
    expect(linkPauseReason(base, 'dec', 1_800_000_000)).toBeNull()
  } finally { rmSync(base, { recursive: true, force: true }) }
})

test('writeLinkState_down为false_删除状态文件', async () => {
  const { writeLinkState } = await loadLinkState()
  const base = tmp()
  try {
    const now = 1_800_000_000
    writeLinkState(base, 'b', true, now, now)
    expect(existsSync(join(base, 'b'))).toBe(true)
    writeLinkState(base, 'b', false, now, now)
    expect(existsSync(join(base, 'b'))).toBe(false)
  } finally { rmSync(base, { recursive: true, force: true }) }
})

test('writeLinkState_目录建不了_不抛', async () => {
  const { writeLinkState } = await loadLinkState()
  const base = tmp()
  try {
    const blocker = join(base, 'afile')
    writeFileSync(blocker, 'x') // 普通文件当目录用，mkdir 必败
    expect(() => writeLinkState(join(blocker, 'sub'), 'b', true, 1, 2)).not.toThrow()
    expect(existsSync(join(blocker, 'sub'))).toBe(false)
  } finally { rmSync(base, { recursive: true, force: true }) }
})

test('writeLinkState_bot名含斜杠或点点_什么都不写', async () => {
  const { writeLinkState } = await loadLinkState()
  const base = tmp()
  try {
    const dir = join(base, 'ls')
    mkdirSync(dir)
    for (const bad of ['a/b', '../evil', '', '-x', '.hidden', 'a b']) {
      expect(() => writeLinkState(dir, bad, true, 1, 2)).not.toThrow()
    }
    expect(readdirSync(dir)).toEqual([])
    expect(readdirSync(base)).toEqual(['ls'])
  } finally { rmSync(base, { recursive: true, force: true }) }
})
