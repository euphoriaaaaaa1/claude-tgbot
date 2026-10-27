// H3b · §13 插件侧 isError —— 黑盒验收
// 只依据 .devflow/INTERFACE-h3b-delivery.md 五(13)：worker 插件对 sent<planned 的响应返回 isError，
// 文本写明哪几段没到，且不含用户正文。
// 手法：起一个假 dispatcher（POST /send 返回 sent<planned），用 MCP stdio 调 worker-plugin 的 reply 工具。
// 不发真 Telegram、不真起 claude；插件只与本机假 dispatcher 通信。
// bug 修复模式：当前插件只看 HTTP 是否 ok，sent<planned 也当成功 → 应为红。
import { test, expect, beforeAll, afterAll } from 'bun:test'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { callPluginTool } from './_plugin_client.ts'

const BODY = 'SECRET-USER-BODY-DO-NOT-ECHO'
let dispatcher: any
let base = ''
let CH = ''

beforeAll(() => {
  CH = mkdtempSync(join(tmpdir(), 'h3b-plugin-'))
  writeFileSync(join(CH, '.api-token'), 'TEST-TOKEN-123')
  dispatcher = Bun.serve({
    port: 0,
    async fetch(req) {
      const u = new URL(req.url)
      if (u.pathname === '/send') {
        // 部分丢段：planned=2, sent=1, 第 1 段（index 1）没到
        return Response.json({ ok: true, planned: 2, sent: 1, dropped: [{ index: 1, reason: 'telegram_4xx' }], message_ids: [111] })
      }
      return Response.json({ ok: true })
    },
  })
  base = `http://127.0.0.1:${dispatcher.port}`
})
afterAll(() => { try { dispatcher?.stop(true) } catch { /* ignore */ } try { rmSync(CH, { recursive: true, force: true }) } catch { /* ignore */ } })

test('插件_reply收到sent小于planned_返回isError_且不含正文', async () => {
  const r = await callPluginTool({
    tool: 'reply',
    args: { text: BODY, chat_id: '123456789' },
    env: {
      CHANNEL_DIR: CH,
      TELEGRAM_WORKER_BOT: 'h3bplug',
      TELEGRAM_WORKER_CHAT_ID: '123456789',
      TELEGRAM_DISPATCHER_URL: base,
      DISPATCHER_URL: base,
      BOT_NAME: 'h3bplug',
    },
  })
  expect(r.isError).toBe(true)                                   // sent<planned → isError
  const text = (r.content ?? []).map(c => c.text ?? '').join('')
  expect(text).not.toContain(BODY)                              // 不得回显用户正文
  expect(text).toMatch(/1/)                                     // 写明没到的段号（index 1）
}, 20000)

test('插件_reply全部送达_不返回isError', async () => {
  const okSrv = Bun.serve({ port: 0, async fetch() { return Response.json({ ok: true, planned: 1, sent: 1, dropped: [], message_ids: [222] }) } })
  try {
    const r = await callPluginTool({
      tool: 'reply',
      args: { text: 'hi', chat_id: '123456789' },
      env: {
        CHANNEL_DIR: CH, TELEGRAM_WORKER_BOT: 'h3bplug', TELEGRAM_WORKER_CHAT_ID: '123456789',
        TELEGRAM_DISPATCHER_URL: `http://127.0.0.1:${okSrv.port}`, DISPATCHER_URL: `http://127.0.0.1:${okSrv.port}`, BOT_NAME: 'h3bplug',
      },
    })
    expect(r.isError ?? false).toBe(false)                     // 全送达 → 不报错（回归）
  } finally { try { okSrv.stop(true) } catch { /* ignore */ } }
}, 20000)
