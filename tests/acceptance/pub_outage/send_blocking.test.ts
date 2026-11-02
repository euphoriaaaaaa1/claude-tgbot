// pub-outage · /send 在线路不通/发送失败时的行为与补发登记（行为清单 #6 #7 #18 #19 #20）
// 真 dispatcher 子进程 + 本机假 Telegram。「判为不通」的做法见 _harness.ts 的 makeLinkDown。
import { test, expect, afterEach } from 'bun:test'
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { GROUP_CHAT, PRIVATE_CHAT, TOKEN, readBytesOrNull, startedPolling, stopAll, type Harness } from './_harness.ts'

afterEach(async () => { await stopAll() })

const isWin = process.platform === 'win32'
const TWO_SEGMENTS = `ALPHA-SECRET-1 ${'甲'.repeat(3000)}\n\nBETA-SECRET-2 ${'乙'.repeat(3000)}` // 超长文本会被拆成两段文字

function resendStateFiles(h: Harness): { name: string; state: any }[] {
  const dir = join(h.channel, '.resend')
  let names: string[] = []
  try { names = readdirSync(dir) } catch { return [] }
  return names.filter(n => n.endsWith('.json')).map(n => ({ name: n, state: JSON.parse(readFileSync(join(dir, n), 'utf8')) }))
}

// ─── #6 ──────────────────────────────────────────────────────────────────────
test('判为不通后_文字_图片_文件三次发送_全部502且link_down_不连Telegram_被晾档案不变', async () => {
  const h = await startedPolling()
  const hangPath = join(h.channel, '.hang-state.json')
  await h.makeLinkDown()
  const hangBefore = readBytesOrNull(hangPath)
  const png = h.makeMedia('p.png', 'png')
  const pdf = h.makeMedia('d.pdf', 'pdf')
  for (const body of [
    { chat_id: PRIVATE_CHAT, text: '断线中的文字' },
    { chat_id: PRIVATE_CHAT, files: [png] },
    { chat_id: PRIVATE_CHAT, files: [pdf] },
  ]) {
    const r = await h.send(body)
    expect([JSON.stringify(body), r.status]).toEqual([JSON.stringify(body), 502])
    expect(r.json.parts[0].ok).toBe(false)
    expect(r.json.parts[0].reason).toBe('link_down')
  }
  expect(h.fake.sendLikeCalls()).toEqual([])
  const hangAfter = readBytesOrNull(hangPath)
  expect(hangAfter === null ? null : hangAfter.toString('hex')).toBe(hangBefore === null ? null : hangBefore.toString('hex'))
}, 150000)

// ─── #7 ──────────────────────────────────────────────────────────────────────
test('线路正常_私聊全送达_400_500_群聊全送达_回包不含resend_parts_不留待补', async () => {
  const h = await startedPolling()
  let forced: number | null = null
  h.fake.sendResponder = () => {
    if (forced === 400) return { status: 400, body: { ok: false, error_code: 400, description: 'Bad Request: chat not found' } }
    if (forced === 500) return { status: 500, body: { ok: false, error_code: 500, description: 'Internal Server Error' } }
    return null
  }
  const ok = await h.send({ chat_id: PRIVATE_CHAT, text: '你好' })
  expect(ok.status).toBe(200)
  expect(ok.json.planned).toBe(1)
  expect(ok.json.sent).toBe(1)
  expect(ok.json.parts[0].ok).toBe(true)
  expect('resend_parts' in ok.json).toBe(false)

  forced = 400
  const r400 = await h.send({ chat_id: PRIVATE_CHAT, text: '会被拒' })
  expect(r400.status).toBe(502)
  expect(r400.json.parts[0].reason).toBe('telegram_4xx')
  expect('resend_parts' in r400.json).toBe(false)

  forced = 500
  const r500 = await h.send({ chat_id: PRIVATE_CHAT, text: '服务端错' })
  expect(r500.status).toBe(502)
  expect(r500.json.parts[0].reason).toBe('telegram_5xx')
  expect('resend_parts' in r500.json).toBe(false)

  forced = null
  const grp = await h.send({ chat_id: GROUP_CHAT, text: '群里说话' })
  expect(grp.status).toBe(200)
  expect(grp.json.sent).toBe(1)
  expect('resend_parts' in grp.json).toBe(false)

  for (const f of resendStateFiles(h)) expect([f.name, f.state.parts]).toEqual([f.name, []])
}, 150000)

// ─── #18 ─────────────────────────────────────────────────────────────────────
test('线路正常时发送失败_停听后立刻发_network_登记resend_parts和一条network待补', async () => {
  const h = await startedPolling()
  await h.fake.setMode('off')
  const r = await h.send({ chat_id: PRIVATE_CHAT, text: '刚断就发' })
  expect(r.status).toBe(502)
  expect(r.json.parts[0].reason).toBe('network')
  expect(r.json.resend_parts).toEqual([0])
  const st = h.readResendState()
  expect(st.parts.length).toBe(1)
  expect(st.parts[0].reason).toBe('network')
}, 90000)

test('线路正常时发送失败_sendMessage读完请求就断开_状态不明_不登记待补_只留一条摘要', async () => {
  const h = await startedPolling()
  h.fake.methodMode.sendMessage = 'cut'
  const r = await h.send({ chat_id: PRIVATE_CHAT, text: '被断开的这段' })
  expect(r.status).toBe(502)
  expect(r.json.parts[0].reason).toBe('network')
  expect('resend_parts' in r.json).toBe(false)
  const st = h.readResendState()
  expect(st).not.toBeNull()
  expect(st.parts).toEqual([])
  expect(Object.keys(st.unknown_keys).length).toBe(1)
  expect(h.stderr()).not.toContain('link_down since=')
}, 90000)

test('线路正常时发送失败_sendMessage接受连接永不回包_约60秒后timeout_不登记待补_只留一条摘要', async () => {
  const h = await startedPolling()
  h.fake.methodMode.sendMessage = 'hang'
  const r = await h.send({ chat_id: PRIVATE_CHAT, text: '永远等不到回包' }, 130000)
  expect(r.status).toBe(502)
  expect(r.json.parts[0].reason).toBe('timeout')
  expect('resend_parts' in r.json).toBe(false)
  const st = h.readResendState()
  expect(st).not.toBeNull()
  expect(st.parts).toEqual([])
  expect(Object.keys(st.unknown_keys).length).toBe(1)
}, 170000)

// ─── #19 ─────────────────────────────────────────────────────────────────────
test('判为不通后私聊发两段文字_回包带resend_parts_状态文件权限正确_日志不含正文和令牌', async () => {
  const h = await startedPolling()
  await h.makeLinkDown()
  const r = await h.send({ chat_id: PRIVATE_CHAT, text: TWO_SEGMENTS })
  expect(r.status).toBe(502)
  expect(r.json.planned).toBe(2)
  expect(r.json.resend_parts).toEqual([0, 1])
  const st = h.readResendState()
  expect(st.parts.length).toBe(2)
  if (!isWin) {
    expect(statSync(h.resendStatePath()).mode & 0o777).toBe(0o600)
    expect(statSync(join(h.channel, '.resend')).mode & 0o777).toBe(0o700)
  }
  expect(h.stderr()).toContain(`resend_recorded chat=${PRIVATE_CHAT} added=2`)
  expect(h.stderr()).not.toContain('ALPHA-SECRET-1')
  expect(h.stderr()).not.toContain('BETA-SECRET-2')
  expect(h.stderr()).not.toContain(TOKEN)
}, 150000)

// ─── #20 ─────────────────────────────────────────────────────────────────────
test('判为不通后向群聊发送_不登记待补_回包不含resend_parts', async () => {
  const h = await startedPolling()
  await h.makeLinkDown()
  const r = await h.send({ chat_id: GROUP_CHAT, text: '群里的话' })
  expect(r.status).toBe(502)
  expect(r.json.parts[0].reason).toBe('link_down')
  expect('resend_parts' in r.json).toBe(false)
  for (const f of resendStateFiles(h)) {
    expect([f.name, f.state.parts]).toEqual([f.name, []])
    expect(f.name).not.toContain('100999000111')
  }
}, 150000)

test('单只停用补发_判为不通后私聊发送_不登记待补_日志写resend_skip_disabled', async () => {
  const h = await startedPolling()
  await h.makeLinkDown()
  mkdirSync(h.disabled, { recursive: true })
  writeFileSync(join(h.disabled, `resend-${h.bot}`), '') // 立即生效，不重启
  const r = await h.send({ chat_id: PRIVATE_CHAT, text: '停用补发之后的话' })
  expect(r.status).toBe(502)
  expect(r.json.parts[0].reason).toBe('link_down')
  expect('resend_parts' in r.json).toBe(false)
  expect((h.readResendState()?.parts ?? []).length).toBe(0)
  expect(h.stderr()).toContain('resend_skip reason=disabled')
}, 150000)
