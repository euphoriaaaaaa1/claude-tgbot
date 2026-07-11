#!/usr/bin/env bun
/**
 * Per-chat worker MCP plugin.
 *
 * - fs-watches the chat's inbox dir → emits `notifications/claude/channel`
 *   in the format the current telegram plugin uses (so the worker's prompt
 *   view matches what the bots see today), then deletes the inbox file.
 * - Also drains existing inbox files at startup.
 * - MCP tools (reply/react/download_attachment/add_group_alias) proxy to
 *   the bot's dispatcher over HTTP — worker never polls Telegram directly.
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { readFileSync, readdirSync, rmSync, watch, mkdirSync, existsSync } from 'fs'
import { join } from 'path'
import { homedir } from 'os'

const BOT = process.env.TELEGRAM_WORKER_BOT || ''
const DISPATCHER = process.env.TELEGRAM_DISPATCHER_URL || 'http://127.0.0.1:17801'
const CHANNEL_DIR = process.env.CHANNEL_DIR || join(homedir(), '.claude', 'channels', BOT)
if (!BOT) {
  process.stderr.write(`worker-plugin: need TELEGRAM_WORKER_BOT\n`)
  process.exit(1)
}

// ─── unified session：一个 worker 收群+私聊，reply 按来源 chat 回 ─────────
// 统一 inbox：dispatcher / director / server 都写 <channel>/inbox/（带 chat_id + scene）。
const UNIFIED_INBOX = join(CHANNEL_DIR, 'inbox')
mkdirSync(UNIFIED_INBOX, { recursive: true })

// 关系数值系统：读 relationship.json 的 prompt_snippet（Python 侧 relationship.py 预算好写入），
// 注入到真人消息前，让 bot 按当前 好感/信任/淫欲/精力 拿捏——尤其精力低时对爱爱没兴致会拒绝。
function relationshipSnippet(): string {
  try {
    const d = JSON.parse(readFileSync(join(CHANNEL_DIR, 'relationship.json'), 'utf8'))
    return (typeof d.prompt_snippet === 'string' && d.prompt_snippet) ? `${d.prompt_snippet}\n\n` : ''
  } catch { return '' }
}

// 遗留 per-chat inbox：self-initiate / moments / scripts 仍写 <channel>/chats/<id>/inbox
// （这些写入方不在本次统一改造范围内）。worker 一并监听现存的这些目录，避免主动消息 /
// 朋友圈回评回归。它们的 payload 同样带 chat_id，reply 路由照常按来源回。
function legacyInboxDirs(): string[] {
  try {
    const chatsDir = join(CHANNEL_DIR, 'chats')
    if (!existsSync(chatsDir)) return []
    return readdirSync(chatsDir)
      .map(name => join(chatsDir, name, 'inbox'))
      .filter(p => existsSync(p))
  } catch { return [] }
}
const INBOX_DIRS = [UNIFIED_INBOX, ...legacyInboxDirs()]

// reply 路由核心：记住"刚送进来那条 inbound 的 chat_id"。worker 串行处理（单 claude
// 会话一次一轮），故 lastChatId 恒为当前正在回应的那条消息的来源 chat。reply 未显式
// 指定 chat_id 时就发回这里——群消息回群、私聊消息回私聊，不会串。
let lastChatId = ''

process.on('unhandledRejection', err => process.stderr.write(`worker-plugin: unhandledRejection: ${err}\n`))
process.on('uncaughtException', err => process.stderr.write(`worker-plugin: uncaughtException: ${err}\n`))

const mcp = new Server(
  { name: 'telegram-worker', version: '1.0.0' },
  { capabilities: { tools: {}, experimental: { 'claude/channel': {} } },
    instructions: [
      'Inbound Telegram messages arrive as notifications/claude/channel. Reply via the reply tool — it goes through the dispatcher to Telegram.',
      '你同时在群聊和私聊里，是同一个你、记忆相通。每条消息带【群聊】/【私聊】标注 + meta.chat_id 告诉你它来自哪。',
      'reply 默认回到最近一条消息所在的聊天（无需指定 chat_id）。',
      '⚠️ 但如果你在同一轮里同时看到来自【不同聊天】的多条消息（比如一条【群聊】+一条【私聊】），回复其中任意一条时【必须】在 reply 里显式带上那条消息 meta 的 chat_id，否则默认值可能把回复发错聊天窗口。',
      '表达分寸随场合：群里有别人，私密或露骨的话别摊开；私聊里更自然放松。',
      '【沉默权 · 你不是必须回复每一条】真人不会有问必答。按你【当下的人设、心情、状态】决定这条要不要回——'
        + '在忙 / 没心情 / 被惹到生气 / 觉得对方的话无聊、重复、催得烦 / 这问题你此刻就是不想答时，'
        + '你可以【直接不调 reply、结束这一轮】——就是装没看见、晾一会儿、避而不答，这比每条都秒答更像活人。'
        + '冷淡、高冷、傲娇、正忙、闹脾气的人设，尤其该多沉默、多冷处理。'
        + '但别走极端把所有消息都无视——该聊、想聊、被打动、或事关重要时，就好好回。沉默是选项，不是常态。',
    ].join('\n') },
)

// ─── inbox delivery ───────────────────────────────────────────────────
function deliverFile(path: string): void {
  let raw: string
  try { raw = readFileSync(path, 'utf8') } catch { return }
  let meta: any
  try { meta = JSON.parse(raw) } catch {
    process.stderr.write(`worker-plugin: bad inbox json ${path}\n`)
    rmSync(path, { force: true })
    return
  }
  const text: string = typeof meta.text === 'string' ? meta.text : ''
  // Build meta block matching server.ts <channel> notification format.
  // NOTE: claude CLI 2.1.92 的 development-channels 会静默 drop `is_bot_sender=true`
  // 的 notification（实测过：false→立刻 enqueue，true→被吞）。所以这里不再传
  // is_bot_sender 字段，改为在 text 内容前加 [from bot @xxx] 标记让 model 知道是
  // 别的 bot 的发言。同样 user 字段避免直接用 `*_bot` username（可能也被识别过滤）。
  const isBotSender = meta.is_bot_sender === true
  const userTag = isBotSender
    ? `peer-bot-${String(meta.from_id ?? '')}`
    : (meta.sender_username ?? meta.from_username ?? String(meta.from_id ?? ''))
  // 场景：优先用 payload 里的 scene，否则按 chat_id 是否以 '-' 开头判定（群 id 为负）。
  const chatIdStr = String(meta.chat_id)
  const scene = (meta.scene === 'group' || meta.scene === 'private')
    ? meta.scene
    : (chatIdStr.startsWith('-') ? 'group' : 'private')
  // reply 路由：记下这条 inbound 的来源 chat，reply 默认发回这里。
  lastChatId = chatIdStr
  const notifMeta: Record<string, unknown> = {
    chat_id: chatIdStr,
    scene,
    user: userTag,
    user_id: String(meta.from_id ?? ''),
    chat_type: meta.chat_type ?? 'private',
    ts: meta.ts,
  }
  if (meta.message_id != null) notifMeta.message_id = String(meta.message_id)
  if (meta.sender_username) notifMeta.sender_username = meta.sender_username
  if (meta.image_path) notifMeta.image_path = meta.image_path
  if (meta.voice_text) notifMeta.voice_text = meta.voice_text
  if (meta.attachment_kind) notifMeta.attachment_kind = meta.attachment_kind
  if (meta.attachment_file_id) notifMeta.attachment_file_id = meta.attachment_file_id
  if (meta.attachment_size != null) notifMeta.attachment_size = String(meta.attachment_size)
  if (meta.attachment_mime) notifMeta.attachment_mime = meta.attachment_mime
  if (meta.attachment_name) notifMeta.attachment_name = meta.attachment_name
  if (meta.reply_to != null) notifMeta.reply_to = String(meta.reply_to)

  // 场景标注前缀 + bot-sender 显式标记：让 model 知道此刻在群还是私聊。
  const sceneTag = scene === 'group' ? '【群聊】' : '【私聊】'
  const body = isBotSender && meta.sender_username
    ? `[from peer bot @${meta.sender_username}]\n${text}`
    : text
  // 真人消息前注入当前关系数值状态——**只私聊**(群聊是导演调度、多人在场，关系块是噪音；
  // peer-bot / 导演 inject 也不注入)。
  const relPrefix = (!isBotSender && scene === 'private') ? relationshipSnippet() : ''
  const content = `${relPrefix}${sceneTag}${body}`
  mcp.notification({
    method: 'notifications/claude/channel',
    params: { content, meta: notifMeta },
  }).catch(err => process.stderr.write(`worker-plugin: notification failed: ${err}\n`))
  rmSync(path, { force: true })
}

// ─── 跨聊天串行化：防 lastChatId 竞态（群+私聊并发回复串窗口，BUG-3）─────────────
// 只延迟"与当前在飞消息不同聊天"的消息；同一聊天的连续消息立即投递（正常单聊零延迟）。
// 在飞消息在 claude 这轮做完（有工具调用后静默 QUIET_MS）或彻底静默 HARD_CAP_MS 后释放，
// 再投递排队的跨聊天消息。这样一次只处理一个聊天，lastChatId 恒对得上，不会串窗口。
let inFlightChatId = ''
let releaseTimer: ReturnType<typeof setTimeout> | null = null
const pendingFiles: string[] = []
const HARD_CAP_MS = 30000   // claude 彻底静默这么久 → 强制释放（防卡死）
const QUIET_MS = 5000       // 有工具调用后静默这么久 → 释放（这轮做完了）

function peekChatId(path: string): string {
  try { return String((JSON.parse(readFileSync(path, 'utf8')) as any).chat_id ?? '') }
  catch { return '' }
}
function scheduleRelease(ms: number): void {
  if (releaseTimer) clearTimeout(releaseTimer)
  releaseTimer = setTimeout(() => { releaseTimer = null; inFlightChatId = ''; pumpPending() }, ms)
}
function pumpPending(): void {
  if (inFlightChatId || pendingFiles.length === 0) return
  const path = pendingFiles.shift()!
  if (!existsSync(path)) { pumpPending(); return }
  inFlightChatId = peekChatId(path) || '_'
  deliverFile(path)
  scheduleRelease(HARD_CAP_MS)
}
function onInbox(path: string): void {
  if (!existsSync(path)) return
  const cid = peekChatId(path) || '_'
  if (!inFlightChatId || cid === inFlightChatId) {
    if (!inFlightChatId) { inFlightChatId = cid; scheduleRelease(HARD_CAP_MS) }
    deliverFile(path)               // 无在飞 或 同聊天 → 立即投递
    return
  }
  pendingFiles.push(path)           // 跨聊天并发 → 排队，等在飞那条处理完
}

function drainInbox(): void {
  for (const dir of INBOX_DIRS) {
    try {
      for (const f of readdirSync(dir).sort()) {
        if (f.endsWith('.json')) onInbox(join(dir, f))
      }
    } catch {}
  }
}

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
  // claude 有工具调用 = 这轮在活动 → 重置释放计时（做完静默 QUIET_MS 才放下一个跨聊天消息）
  if (inFlightChatId) scheduleRelease(QUIET_MS)
  try {
    switch (req.params.name) {
      case 'reply': {
        // 路由：显式 chat_id 优先，否则回到刚处理那条 inbound 的来源 chat。
        const targetChat = String(args.chat_id || lastChatId)
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
        await postJson('/react', { chat_id: String(args.chat_id || lastChatId), message_id: args.message_id, emoji: args.emoji })
        return { content: [{ type: 'text', text: 'reacted' }] }
      }
      case 'download_attachment': {
        const out = await postJson('/download', { file_id: args.file_id, chat_id: String(args.chat_id || lastChatId) })
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

// fs-watch: debounce-free; rename events fire when finalized rename happens.
// 监听统一 inbox + 现存的遗留 per-chat inbox（启动时快照；新增 chat 目录在 worker
// 重启后才纳入监听，但真实 Telegram 流量都走统一 inbox，遗留写入方只碰已存在的私聊目录）。
for (const dir of INBOX_DIRS) {
  if (!existsSync(dir)) continue
  watch(dir, (event, fname) => {
    if (!fname || !fname.endsWith('.json')) return
    const path = join(dir, fname)
    // small delay — rename completion
    setTimeout(() => { if (existsSync(path)) onInbox(path) }, 20)
  })
}
// startup drain (covers messages that arrived while worker was down)
setTimeout(drainInbox, 500)

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
