// pub-outage · 启动闸与总开关（行为清单 #4 #5 #11）
// 真 dispatcher 子进程 + 本机假 Telegram；HOME/CHANNEL_DIR/LINK_STATE_DIR/DISABLED_DIR 全在临时目录。
import { test, expect, afterEach } from 'bun:test'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { PRIVATE_CHAT, sleep, startedHarness, stopAll, waitFor } from './_harness.ts'

afterEach(async () => { await stopAll() })

test('断线中启动_闸不开_状态文件新鲜_恢复后25秒内开始收消息且状态文件被清', async () => {
  const h = await startedHarness({ fakeMode: 'off' })
  const startedAt = Date.now()
  await h.waitHttp(30000)
  // 切换之前（约第 19 秒）：线路状态文件存在且 checked_at 新鲜；/health 照常
  await waitFor(() => existsSync(h.linkStateFile()), 20000, '断线启动期间出现线路状态文件')
  await sleep(Math.max(0, 19_000 - (Date.now() - startedAt)))
  const content = readFileSync(h.linkStateFile(), 'utf8')
  const m = /^down_since=(\d+) checked_at=(\d+)\n$/.exec(content)
  expect(m).not.toBeNull()
  const nowSec = Math.floor(Date.now() / 1000)
  expect(Math.abs(nowSec - Number(m![2]))).toBeLessThanOrEqual(20)
  const health = await h.health()
  expect(health.status).toBe(200)
  expect(h.fake.methods).not.toContain('getUpdates')
  expect(h.stderr()).not.toContain('polling as')
  // 切回正常
  await h.fake.setMode('normal')
  await h.waitStderr('polling as', 25000)
  await waitFor(() => !existsSync(h.linkStateFile()), 3000, 'polling as 之后线路状态文件被清掉')
}, 90000)

test('线路正常启动_10秒内开始收消息_第一个请求是getMe_之后无状态文件', async () => {
  const h = await startedHarness()
  await h.waitStderr('polling as', 10000)
  expect(h.fake.methods[0]).toBe('getMe')
  await sleep(1500)
  expect(existsSync(h.linkStateFile())).toBe(false)
}, 40000)

test('总开关linkGateEnabled为false_旧文件被清_发送仍报network_不出现link_down', async () => {
  const now = Date.now()
  const h = await startedHarness({
    fakeMode: 'off',
    access: { linkGateEnabled: false },
    preStart: hh => {
      mkdirSync(hh.linkState, { recursive: true })
      writeFileSync(hh.linkStateFile(), `down_since=${Math.floor(now / 1000) - 5} checked_at=${Math.floor(now / 1000)}\n`)
      mkdirSync(join(hh.channel, '.resend'), { recursive: true })
      const old = now - 13 * 3600_000 - 60_000
      writeFileSync(join(hh.channel, '.resend', `${PRIVATE_CHAT}.json`), JSON.stringify({
        v: 2, chat_id: PRIVATE_CHAT, epoch: 0,
        parts: [{ key: '0123456789abcdef', kind: 'text', text: '很久以前', reason: 'link_down', at_ms: old }],
        batch_ms: null, rounds: 0, injected_ms: null, inbox_key: null, taken_ms: null, listed_total: 0, unknown_keys: {}, cooldown_until_ms: null,
      }))
    },
  })
  await h.waitHttp(30000)
  const r = await h.send({ chat_id: PRIVATE_CHAT, text: '开关关着' })
  expect(r.status).toBe(502)
  expect(r.json.parts[0].reason).toBe('network')
  expect('resend_parts' in r.json).toBe(false)
  expect(h.stderr()).not.toContain('link_down since=')
  await waitFor(() => !existsSync(h.linkStateFile()), 20000, '预置的线路状态文件被清掉')
  await waitFor(() => !existsSync(h.resendStatePath()), 20000, '过期的补发状态文件被清掉')
  expect(h.stderr()).not.toContain('link_down since=')
}, 90000)

test('总开关写成字符串false不算关_仍启用线路判断', async () => {
  const h = await startedHarness({ fakeMode: 'off', access: { linkGateEnabled: 'false' } })
  await h.waitHttp(30000)
  await waitFor(() => existsSync(h.linkStateFile()), 20000, '字符串 "false" 视为开：断线启动应写线路状态文件')
}, 60000)
