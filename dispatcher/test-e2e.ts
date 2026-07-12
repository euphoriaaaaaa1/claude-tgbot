#!/usr/bin/env bun
/**
 * test-e2e.ts — worker-manager 端到端自测（不碰 Telegram，跨平台）。
 *
 * 验证整条新链路：inbox 文件 → manager 组装 → claude stdin → claude 调 MCP
 * reply 工具 → HTTP /send 到 mock dispatcher。再测 slash(/compact) 与崩溃 resume。
 *
 * 跑法（macOS/Windows 相同）：
 *   cd dispatcher && bun install && bun test-e2e.ts
 * 前提：claude CLI 已装并配好 provider（settings.json 或已登录）。
 * 会创建 ~/.claude/channels/__e2etest__（结束不删，方便查看日志；可手动删）。
 */
import { mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'fs'
import { join } from 'path'
import { homedir } from 'os'

const BOT = '__e2etest__'
const PORT = 17899
const CHAN = join(homedir(), '.claude', 'channels', BOT)

// ─── 测试 channel 目录 ───
mkdirSync(join(CHAN, 'inbox'), { recursive: true })
writeFileSync(join(CHAN, 'CLAUDE.md'), [
  '# 测试机器人',
  '你是端到端测试机器人。收到任何消息，必须调用 reply 工具，text 固定回「E2E-OK」。',
  '不要说别的话，不要问问题。',
].join('\n'))

// ─── mock dispatcher：收 /send 记账 ───
const received: any[] = []
const server = Bun.serve({
  port: PORT, hostname: '127.0.0.1',
  async fetch(req) {
    const url = new URL(req.url)
    if (url.pathname === '/send') {
      const body = await req.json()
      received.push(body)
      console.log(`[mock] 收到 /send: ${JSON.stringify(body).slice(0, 120)}`)
      return Response.json({ ok: true, message_ids: [1] })
    }
    return Response.json({ ok: true })
  },
})

// ─── 起 manager（env 先设好再 import，模块读 env）───
process.env.BOT_NAME = BOT
process.env.CHANNEL_DIR = CHAN
process.env.DISPATCHER_PORT = String(PORT)
const { getManager, BOT_NAMESPACES } = await import('./worker-manager.ts')
BOT_NAMESPACES[BOT] = '550e8400-e29b-41d4-a716-446655449999' // 测试专用命名空间
const mgr = getManager()

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
async function waitFor(cond: () => boolean, ms: number, label: string): Promise<boolean> {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { if (cond()) return true; await sleep(1000) }
  console.log(`❌ 超时: ${label}`)
  return false
}

let pass = 0, fail = 0
const check = (ok: boolean, label: string) => { console.log(`${ok ? '✅' : '❌'} ${label}`); ok ? pass++ : fail++ }

// ─── T1: 冷启动 + inbox → reply ───
console.log('── T1: inbox → stdin → claude → MCP reply → /send ──')
const ms1 = Date.now()
writeFileSync(join(CHAN, 'inbox', `test-${ms1}.json`), JSON.stringify({
  text: '你好，请回复', chat_id: '10001', from_id: '10001',
  from_username: 'tester', chat_type: 'private', scene: 'private',
  is_bot_sender: false, ts: new Date().toISOString(), message_id: String(ms1),
}))
await mgr.ensure()
check(await waitFor(() => received.length >= 1, 120_000, '等 reply 到达 mock /send'), 'T1 reply 送达 /send')
check(received.some(r => String(r.text ?? '').includes('E2E-OK')), 'T1 回复内容含 E2E-OK')
check(received.some(r => String(r.chat_id) === '10001'), 'T1 chat_id 路由正确(=10001)')

// ─── T2: slash /compact ───
console.log('── T2: sendSlash(/compact) → compact_boundary ──')
mgr.sendSlash('/compact')
const streamLog = join(CHAN, 'logs', 'stream.jsonl')
check(await waitFor(() =>
  existsSync(streamLog) && readFileSync(streamLog, 'utf8').includes('compact_boundary'),
  120_000, '等 compact_boundary'), 'T2 /compact 生效')

// ─── T3: 崩溃自动 resume ───
console.log('── T3: kill -9 worker → 自动 --resume → 记忆连续 ──')
const pid = (mgr.status() as any).pid
if (pid) process.kill(pid, 'SIGKILL')
await sleep(3000)
const before = received.length
const ms2 = Date.now()
writeFileSync(join(CHAN, 'inbox', `test-${ms2}.json`), JSON.stringify({
  text: '又是我，请再回复一次', chat_id: '10001', from_id: '10001',
  from_username: 'tester', chat_type: 'private', scene: 'private',
  is_bot_sender: false, ts: new Date().toISOString(), message_id: String(ms2),
}))
check(await waitFor(() => received.length > before, 180_000, '等 resume 后的 reply'), 'T3 崩溃后自动 resume 并回复')

// ─── 收尾 ───
console.log(`\n════ 结果: ${pass} 过 / ${fail} 挂 ════`)
console.log(`观测日志: ${join(CHAN, 'logs', 'chat.log')}`)
mgr.kill({ intentional: true })
server.stop()
// 清理测试 session（channel 目录保留供查看）
process.exit(fail === 0 ? 0 : 1)
