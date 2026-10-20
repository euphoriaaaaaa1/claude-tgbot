/**
 * 承诺兑现 · 独立服务（只用于黑盒验收与本地排查，**不在生产跑**）。
 *
 * 生产里这套逻辑挂在 dispatcher 进程内（dispatcher.ts 的 /promise 端点 + 60s tick），
 * 与这里调的是同一个 createPromiseRuntime / 同一套接线件（promise_wire.ts）。
 * 独立起一份是为了让验收测试能拿假 bot 名、假 token、临时目录把它整个隔离起来跑，
 * 不用启动真的 Telegram 轮询、不碰任何真实通道目录。
 *
 * 安全闸：没有 PROMISE_TEST_MODE=1 直接拒绝启动——这个入口永远不可能对着生产数据跑，
 * 也就不可能凭空多出一个 tick 去重复兑现真实用户的承诺。
 */
import { mkdirSync, renameSync, writeFileSync } from 'fs'
import { join } from 'path'
import { createPromiseRuntime } from './promise_runtime'
import {
  makeAsleepProbe, makeBotDisabledCheck, makeInboxRetract, makeSessionScanner, makeStopCheck, makeTestPauseCheck,
  resolvePromiseChannelDir,
} from './promise_wire'

const TEST_MODE = process.env.PROMISE_TEST_MODE === '1'
const BOT_NAME = process.env.BOT_NAME || ''
const RAW_CHANNEL_DIR = process.env.CHANNEL_DIR || ''
const PORT = Number(process.env.DISPATCHER_PORT || '0')

if (!TEST_MODE) {
  process.stderr.write('promise_service: 拒绝启动——只允许在 PROMISE_TEST_MODE=1 的隔离环境里跑，生产走 dispatcher 内置 tick\n')
  process.exit(2)
}
if (!RAW_CHANNEL_DIR || !BOT_NAME || !Number.isFinite(PORT) || PORT <= 0) {
  process.stderr.write('promise_service: missing env (CHANNEL_DIR / BOT_NAME / DISPATCHER_PORT)\n')
  process.exit(2)
}
// 停用开关（.disabled/<bot>、.disabled/promise-<bot>）没设 DISABLED_DIR 时会回落真实家目录下的
// ~/.claude/dispatcher/.disabled——测试结果不得受真实运行状态左右，缺它就拒绝启动（复查 r5 建议 4）
if (!process.env.DISABLED_DIR) {
  process.stderr.write('promise_service: missing env DISABLED_DIR（测试模式必须指向沙箱内的停用开关目录）\n')
  process.exit(2)
}

const CHANNEL_DIR = resolvePromiseChannelDir(RAW_CHANNEL_DIR, BOT_NAME)
const TICK_MS = Number(process.env.PROMISE_TICK_MS || '60000')

/** 与 dispatcher.ts:writeInbox 同形：unified inbox、文件名带 chatId 前缀、tmp→rename */
function writeInbox(chatId: string, messageKey: string, meta: Record<string, unknown>): void {
  const dir = join(CHANNEL_DIR, 'inbox')
  mkdirSync(dir, { recursive: true })
  const key = `${chatId}_${messageKey}`.replace(/[^a-zA-Z0-9_-]/g, '_')
  const tmp = join(dir, `${key}.json.tmp`)
  const fin = join(dir, `${key}.json`)
  writeFileSync(tmp, JSON.stringify(meta, null, 2), { mode: 0o600 })
  renameSync(tmp, fin)
}

// 暂停闸只认沙箱内/显式指定的测试文件（PROMISE_PAUSE_FILE），不读真实家目录的暂停文件（见 makeTestPauseCheck）
const isPaused = makeTestPauseCheck()

const botDisabled = makeBotDisabledCheck(BOT_NAME)
const promise = createPromiseRuntime({
  channelDir: CHANNEL_DIR,
  inject: writeInbox,
  retract: makeInboxRetract(CHANNEL_DIR),
  ...makeSessionScanner(CHANNEL_DIR, BOT_NAME),   // 送达确认：从发出时的偏移扫到文件末尾（R5）
  probeAsleep: makeAsleepProbe(BOT_NAME),
  isPaused: () => isPaused() ?? botDisabled(),     // 暂停文件 或 bot 停用开关 .disabled/<bot>（E9 / §8-2）
  isStopped: makeStopCheck(BOT_NAME),
  // 独立服务永不拉活 worker：测试环境里拉活等于去碰真实 worker 会话
  log: line => process.stderr.write(`${line}\n`),
})

Bun.serve({
  port: PORT,
  hostname: '127.0.0.1',
  async fetch(req) {
    const url = new URL(req.url)
    if (req.method !== 'POST' || url.pathname !== '/promise') return new Response('not found', { status: 404 })
    let body: unknown
    try { body = await req.json() } catch { return new Response('bad json', { status: 400 }) }
    try {
      const r = await promise.handle(body)
      return Response.json(r.body, { status: r.status })
    } catch (e) {
      process.stderr.write(`promise_service: handle failed reason=${e instanceof Error ? e.name : 'unknown'}\n`)
      return Response.json({ ok: false, error: 'store_failed' }, { status: 500 })
    }
  },
})

const guard = async () => {
  try { await promise.tick() } catch (e) {
    process.stderr.write(`promise_service: tick failed reason=${e instanceof Error ? e.name : 'unknown'}\n`)
  }
}
void guard()                       // 启动即补一轮：停机期间错过的到期要立刻补上
setInterval(guard, TICK_MS)
process.stderr.write(`promise_service: listening on 127.0.0.1:${PORT} channel=${CHANNEL_DIR}\n`)
