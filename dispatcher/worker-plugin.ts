#!/usr/bin/env bun
/**
 * Worker MCP plugin — 纯工具面（reply/react/download_attachment/add_group_alias）。
 *
 * 入站消息不再走本插件（旧架构：fs.watch inbox → notifications/claude/channel）——
 * 现在由 dispatcher 里的 worker-manager 读 inbox、组装后直接写 claude stdin。
 * 本插件只保留出站工具，全部经 dispatcher HTTP 代理，worker 从不直连 Telegram。
 *
 * reply 默认路由：worker-manager 每次注入消息前把来源 chat_id 原子写到
 * <CHANNEL_DIR>/.last-chat-id；manager 严格串行（一次一轮），读到的恒为当前轮来源。
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { readFileSync } from 'fs'
import { join } from 'path'
import { homedir } from 'os'

const BOT = process.env.TELEGRAM_WORKER_BOT || ''
const DISPATCHER = process.env.TELEGRAM_DISPATCHER_URL || 'http://127.0.0.1:17801'
const CHANNEL_DIR = process.env.CHANNEL_DIR || join(homedir(), '.claude', 'channels', BOT)
if (!BOT) {
  process.stderr.write(`worker-plugin: need TELEGRAM_WORKER_BOT\n`)
  process.exit(1)
}

// reply 默认回到"刚注入那条消息"的来源 chat（manager 写的 sidecar 文件）
function lastChatId(): string {
  try { return readFileSync(join(CHANNEL_DIR, '.last-chat-id'), 'utf8').trim() } catch { return '' }
}

process.on('unhandledRejection', err => process.stderr.write(`worker-plugin: unhandledRejection: ${err}\n`))
process.on('uncaughtException', err => process.stderr.write(`worker-plugin: uncaughtException: ${err}\n`))

const mcp = new Server(
  { name: 'telegram-worker', version: '2.0.0' },
  { capabilities: { tools: {} },
    instructions: [
      'Inbound Telegram messages arrive as user messages tagged with a <channel ...> meta block. Reply via the reply tool — it goes through the dispatcher to Telegram.',
      '你同时在群聊和私聊里，是同一个你、记忆相通。每条消息带【群聊】/【私聊】标注 + <channel> 的 chat_id 告诉你它来自哪。',
      'reply 默认回到最近一条消息所在的聊天（无需指定 chat_id）。',
      '⚠️ 但如果你在同一轮里同时看到来自【不同聊天】的多条消息（比如一条【群聊】+一条【私聊】），回复其中任意一条时【必须】在 reply 里显式带上那条消息 <channel> 的 chat_id，否则默认值可能把回复发错聊天窗口。',
      '表达分寸随场合：群里有别人，私密或露骨的话别摊开；私聊里更自然放松。',
      '【沉默权 · 你不是必须回复每一条】真人不会有问必答。按你【当下的人设、心情、状态】决定这条要不要回——'
        + '在忙 / 没心情 / 被惹到生气 / 觉得对方的话无聊、重复、催得烦 / 这问题你此刻就是不想答时，'
        + '你可以【直接不调 reply、结束这一轮】——就是装没看见、晾一会儿、避而不答，这比每条都秒答更像活人。'
        + '冷淡、高冷、傲娇、正忙、闹脾气的人设，尤其该多沉默、多冷处理。'
        + '但别走极端把所有消息都无视——该聊、想聊、被打动、或事关重要时，就好好回。沉默是选项，不是常态。',
    ].join('\n') },
)

// ─── HTTP helper ─────────────────────────────────────────────────────
async function postJson(path: string, body: any): Promise<any> {
  const r = await fetch(`${DISPATCHER}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`dispatcher ${path} ${r.status}: ${await r.text().catch(() => '')}`)
  return r.json()
}

// ─── tools ───────────────────────────────────────────────────────────
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    { name: 'reply', description: '回复消息。默认回到你正在处理的那条消息所在的聊天（群→群、私聊→私聊）；只有要主动跨聊天发时才传 chat_id。text 必填；可选 chat_id、reply_to、files、as_voice、voice_text、voice_emotion、voice_instruct。',
      inputSchema: { type: 'object', properties: {
        text: { type: 'string' }, chat_id: { type: 'string' }, reply_to: { type: 'string' },
        files: { type: 'array', items: { type: 'string' } },
        as_voice: { type: 'boolean' }, voice_text: { type: 'string' },
        voice_emotion: { type: 'string', enum: ['HAPPY','SAD','ANGRY','NEUTRAL','FEARFUL','SURPRISED','DISGUSTED'] },
        voice_instruct: { type: 'string' },
      }, required: ['text'] } },
    { name: 'react', description: 'Add emoji reaction to a message in this chat.',
      inputSchema: { type: 'object', properties: { message_id: { type: 'string' }, emoji: { type: 'string' } }, required: ['message_id','emoji'] } },
    { name: 'download_attachment', description: 'Download an attachment by file_id (from inbound meta). Returns local path.',
      inputSchema: { type: 'object', properties: { file_id: { type: 'string' } }, required: ['file_id'] } },
    { name: 'add_group_alias', description: 'Persist a new group alias (self or other). Writes bot-level access.json.',
      inputSchema: { type: 'object', properties: {
        group_id: { type: 'string' }, alias: { type: 'string' },
        kind: { type: 'string', enum: ['self','other'] },
      }, required: ['group_id','alias','kind'] } },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  const args = (req.params.arguments ?? {}) as Record<string, unknown>
  try {
    switch (req.params.name) {
      case 'reply': {
        // 路由：显式 chat_id 优先，否则回到刚注入那条 inbound 的来源 chat。
        const targetChat = String(args.chat_id || lastChatId())
        const out = await postJson('/send', {
          chat_id: targetChat,
          text: args.text,
          reply_to: args.reply_to,
          files: args.files,
          as_voice: args.as_voice,
          voice_text: args.voice_text,
          voice_emotion: args.voice_emotion,
          voice_instruct: args.voice_instruct,
        })
        const ids: number[] = out.message_ids ?? []
        return { content: [{ type: 'text', text: ids.length === 1 ? `sent (id: ${ids[0]})` : `sent ${ids.length} parts (ids: ${ids.join(', ')})` }] }
      }
      case 'react': {
        await postJson('/react', { chat_id: String(args.chat_id || lastChatId()), message_id: args.message_id, emoji: args.emoji })
        return { content: [{ type: 'text', text: 'reacted' }] }
      }
      case 'download_attachment': {
        const out = await postJson('/download', { file_id: args.file_id, chat_id: String(args.chat_id || lastChatId()) })
        return { content: [{ type: 'text', text: String(out.path) }] }
      }
      case 'add_group_alias': {
        const out = await postJson('/add_group_alias', { group_id: args.group_id, alias: args.alias, kind: args.kind })
        return { content: [{ type: 'text', text: `ok: ${out.field} += ${out.alias} (${out.total} total)` }] }
      }
      default:
        return { content: [{ type: 'text', text: `unknown tool: ${req.params.name}` }], isError: true }
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return { content: [{ type: 'text', text: `${req.params.name} failed: ${msg}` }], isError: true }
  }
})

await mcp.connect(new StdioServerTransport())

// Shutdown on stdio EOF (claude closed the MCP transport)
let shuttingDown = false
function shutdown(): void {
  if (shuttingDown) return
  shuttingDown = true
  process.stderr.write('worker-plugin: shutting down\n')
  setTimeout(() => process.exit(0), 1000)
}
process.stdin.on('end', shutdown)
process.stdin.on('close', shutdown)
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
