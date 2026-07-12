#!/usr/bin/env bun
/**
 * Per-bot Telegram dispatcher — grammY long-poll + HTTP server.
 *
 * Runs one process per bot (e.g. chenlulu). Owns the Telegram bot token,
 * performs gate+route, writes inbound messages to inbox files, and exposes
 * /send /react /download /add_group_alias /ensure_worker /status /inject.
 *
 * Worker (headless claude, stream-json) 由同进程的 worker-manager.ts 托管：
 * inbox → stdin 注入，崩溃自动 --resume，跨平台（macOS/Windows/Linux 无 tmux）。
 */

import { Bot, GrammyError, InputFile, type Context } from 'grammy'
import type { ReactionTypeEmoji } from 'grammy/types'
import {
  readFileSync, writeFileSync, mkdirSync, statSync, renameSync, appendFileSync, existsSync,
} from 'fs'
import { join, extname } from 'path'
import { homedir } from 'os'
import { getManager, unifiedSessionUuid } from './worker-manager.ts'

// ─── env ──────────────────────────────────────────────────────────────
const CHANNEL_DIR = process.env.CHANNEL_DIR || ''
const TOKEN = process.env.TELEGRAM_BOT_TOKEN || ''
const DISPATCHER_PORT = Number(process.env.DISPATCHER_PORT || '17801')
const BOT_NAME = process.env.BOT_NAME || ''

// ─── peer dispatchers (for bot-to-bot cross-inject; groups only) ─────
type PeerConfig = { name: string; url: string; username: string }
const PEERS: PeerConfig[] = (() => {
  try {
    const raw = process.env.PEERS_JSON
    if (!raw) return []
    const arr = JSON.parse(raw) as PeerConfig[]
    return arr.filter(p => p && p.name && p.name !== BOT_NAME)
  } catch (e) {
    process.stderr.write(`dispatcher[${BOT_NAME}]: bad PEERS_JSON: ${e}\n`)
    return []
  }
})()
let sourceBotUserId: number | undefined

// Dedup key: chat_id|from_id|message_id → ts. 同一条消息通过 peer_inbound 和
// Telegram polling 都会到（现代 Bot API + privacy off 的群 bot 可互相可见），
// 这里保证只处理第一次到达的那路径。5 秒窗口足够覆盖双路径时差。
const __dedup = new Map<string, number>()
const DEDUP_TTL_MS = 5000
function __isDupeAndMark(chatId: string, fromId: string, msgId: string): boolean {
  const now = Date.now()
  // GC 老 entry（lazy）
  if (__dedup.size > 500) {
    for (const [k, ts] of __dedup) if (now - ts > DEDUP_TTL_MS) __dedup.delete(k)
  }
  const key = `${chatId}|${fromId}|${msgId}`
  const prev = __dedup.get(key)
  if (prev != null && now - prev < DEDUP_TTL_MS) return true
  __dedup.set(key, now)
  return false
}
if (!CHANNEL_DIR || !TOKEN || !BOT_NAME) {
  process.stderr.write(`dispatcher: missing env (CHANNEL_DIR=${!!CHANNEL_DIR}, TELEGRAM_BOT_TOKEN=${!!TOKEN}, BOT_NAME=${!!BOT_NAME})\n`)
  process.exit(1)
}

const ACCESS_FILE = join(CHANNEL_DIR, 'access.json')
const CHATS_DIR = join(CHANNEL_DIR, 'chats')
const MEDIA_DIR_DM = join(homedir(), '.claude', 'channels', 'media', BOT_NAME, 'telegram')
const MEDIA_DIR_GROUP = join(homedir(), '.claude', 'channels', 'media', BOT_NAME, 'telegram-group')
const GROUP_TRANSCRIPTS_DIR = join(homedir(), '.claude', 'channels', 'group_transcripts')
mkdirSync(CHATS_DIR, { recursive: true })
mkdirSync(MEDIA_DIR_DM, { recursive: true })
mkdirSync(MEDIA_DIR_GROUP, { recursive: true })
mkdirSync(GROUP_TRANSCRIPTS_DIR, { recursive: true })

process.on('unhandledRejection', err => process.stderr.write(`dispatcher: unhandledRejection: ${err}\n`))
process.on('uncaughtException', err => process.stderr.write(`dispatcher: uncaughtException: ${err}\n`))

// ─── access.json ──────────────────────────────────────────────────────
type GroupPolicy = {
  requireMention: boolean
  allowFrom: string[]
  selfAliases?: string[]
  otherBotUsernames?: string[]
  otherBotAliases?: string[]
}
type Access = {
  dmPolicy: 'pairing' | 'allowlist' | 'disabled'
  allowFrom: string[]
  groups: Record<string, GroupPolicy>
  pending: Record<string, unknown>
  mentionPatterns?: string[]
  ackReaction?: string
  replyToMode?: 'off' | 'first' | 'all'
  textChunkLimit?: number
  chunkMode?: 'length' | 'newline'
  splitOnParagraph?: boolean
  paragraphDelay?: number
  voiceId?: string
}
function loadAccess(): Access {
  try {
    const p = JSON.parse(readFileSync(ACCESS_FILE, 'utf8')) as Partial<Access>
    return {
      dmPolicy: p.dmPolicy ?? 'allowlist',
      allowFrom: p.allowFrom ?? [],
      groups: p.groups ?? {},
      pending: p.pending ?? {},
      mentionPatterns: p.mentionPatterns,
      ackReaction: p.ackReaction,
      replyToMode: p.replyToMode,
      textChunkLimit: p.textChunkLimit,
      chunkMode: p.chunkMode,
      splitOnParagraph: p.splitOnParagraph,
      paragraphDelay: p.paragraphDelay,
      voiceId: p.voiceId,
    }
  } catch {
    return { dmPolicy: 'disabled', allowFrom: [], groups: {}, pending: {} }
  }
}

const bot = new Bot(TOKEN)
let botUsername = ''

// ─── gate / mention / group router (ported verbatim) ─────────────────
function isMentioned(ctx: Context, extraPatterns?: string[]): boolean {
  const entities = ctx.message?.entities ?? ctx.message?.caption_entities ?? []
  const text = ctx.message?.text ?? ctx.message?.caption ?? ''
  for (const e of entities) {
    if (e.type === 'mention') {
      const mentioned = text.slice(e.offset, e.offset + e.length)
      if (mentioned.toLowerCase() === `@${botUsername}`.toLowerCase()) return true
    }
    if (e.type === 'text_mention' && e.user?.is_bot && e.user.username === botUsername) return true
  }
  if (ctx.message?.reply_to_message?.from?.username === botUsername) return true
  for (const pat of extraPatterns ?? []) {
    try { if (new RegExp(pat, 'i').test(text)) return true } catch {}
  }
  // Plain-text fallback: covers synthetic peer-inbound (no Telegram entities)
  // and real messages where @username is typed as plain text not parsed as entity.
  if (botUsername && new RegExp(`@${botUsername}\\b`, 'i').test(text)) return true
  return false
}
function hasExplicitEntityMention(ctx: Context): boolean {
  const entities = ctx.message?.entities ?? ctx.message?.caption_entities ?? []
  const text = ctx.message?.text ?? ctx.message?.caption ?? ''
  for (const e of entities) {
    if (e.type === 'mention') {
      const mentioned = text.slice(e.offset, e.offset + e.length)
      if (mentioned.toLowerCase() === `@${botUsername}`.toLowerCase()) return true
    }
    if (e.type === 'text_mention' && e.user?.is_bot && e.user.username === botUsername) return true
  }
  return false
}
function decideGroupReply(ctx: Context, policy: GroupPolicy, extraPatterns?: string[]): 'reply' | 'drop' {
  const text = ctx.message?.text ?? ctx.message?.caption ?? ''
  const isBotSender = ctx.from?.is_bot === true
  const myExplicit = isMentioned(ctx, extraPatterns)
  const myAliasHit = (policy.selfAliases ?? []).some(a => a.length > 0 && text.includes(a))
  const otherMentioned =
    (policy.otherBotUsernames ?? []).some(u => u.length > 0 && text.toLowerCase().includes(u.toLowerCase())) ||
    (policy.otherBotAliases ?? []).some(a => a.length > 0 && text.includes(a))
  // Bot sender — strict alias/mention mode (prevent echo storms; budget check applies separately in handleInbound)
  if (isBotSender) return (myExplicit || myAliasHit) ? 'reply' : 'drop'
  // Human sender — default to reply unless clearly addressed to another bot only
  if (myExplicit || myAliasHit) return 'reply'
  if (otherMentioned) return 'drop'
  return 'reply'
}

// ─── bot-to-bot auto-chat budget/lock state (in-memory per chat) ──────
// Budget: 10 rounds × 10 min window, whichever exhausts first → drop bot-sender
// Hard-stop phrase → 30 min lock. Continue phrase or unlocked human msg → reset.
const BOT_CHAT_BUDGET_MAX = 10
const BOT_CHAT_WINDOW_MS = 10 * 60 * 1000
const BOT_CHAT_LOCK_MS = 30 * 60 * 1000
const HARD_STOP_PATTERNS: RegExp[] = [
  /^\s*停\s*$/,
  /别聊了/,
  /\/stop\b/i,
  /等会再聊/,
  /等一下再聊/,
  /安静一下/,
  /安静会儿/,
  /你俩安静/,
  /你们安静/,
  /不聊了/,
  /别说了/,
  /闭嘴/,
]
const CONTINUE_PATTERNS: RegExp[] = [
  /你们先聊/,
  /你俩先聊/,
  /你俩先说/,
  /你们继续/,
  /你俩继续/,
]
type BotChatState = { budget: number; windowStart: number; lockedUntil: number }
const chatChatState = new Map<string, BotChatState>()
function getChatState(chatId: string): BotChatState {
  let s = chatChatState.get(chatId)
  if (!s) { s = { budget: BOT_CHAT_BUDGET_MAX, windowStart: 0, lockedUntil: 0 }; chatChatState.set(chatId, s) }
  return s
}
function onHumanGroupMsg(chatId: string, text: string): void {
  const s = getChatState(chatId)
  const now = Date.now()
  if (HARD_STOP_PATTERNS.some(p => p.test(text))) {
    s.budget = 0; s.windowStart = now; s.lockedUntil = now + BOT_CHAT_LOCK_MS
    process.stderr.write(`dispatcher[${BOT_NAME}]: bot-chat HARD_STOP on chat ${chatId} (lock 30min)\n`)
    return
  }
  if (CONTINUE_PATTERNS.some(p => p.test(text))) {
    s.budget = BOT_CHAT_BUDGET_MAX; s.windowStart = now; s.lockedUntil = 0
    process.stderr.write(`dispatcher[${BOT_NAME}]: bot-chat KICK on chat ${chatId} (budget=${BOT_CHAT_BUDGET_MAX})\n`)
    return
  }
  // normal human speech: only reset if not currently locked
  if (now >= s.lockedUntil) {
    s.budget = BOT_CHAT_BUDGET_MAX; s.windowStart = now; s.lockedUntil = 0
  }
}
// Returns true if bot-sender msg should proceed to worker (consumes 1 budget).
function allowBotSenderRoute(chatId: string): boolean {
  const s = getChatState(chatId)
  const now = Date.now()
  if (now < s.lockedUntil) return false
  if (s.windowStart === 0 || now - s.windowStart > BOT_CHAT_WINDOW_MS) return false
  if (s.budget <= 0) return false
  s.budget -= 1
  return true
}

// ─── 导演模式开关（默认关=现有行为一字不变）───────────────────────
// 文件 ~/.claude/dispatcher/.director-mode/<chatId> 存在 = 该群交给中央导演(director.py)调度：
// bot 不自己判断该不该回、只被 [director] inbox 驱动，且不再 peer 互推（拔掉去中心化抢答源）。
// 文件不存在 = 关 = 现有去中心化群聊行为完全不变（默认安全态）。删文件即秒级回退。
const DIRECTOR_MODE_DIR = join(homedir(), '.claude', 'dispatcher', '.director-mode')
function directorModeOn(chatId: string): boolean {
  try { return existsSync(join(DIRECTOR_MODE_DIR, chatId)) } catch { return false }
}

// 跨场景实时记忆(crossmem)已作废：unified session 群+私聊同一会话、记忆相通，
// 不再需要跨场景注入桥。相关开关/调用已移除（cross_scene.build_block 也已置空）。

type GateResult = { action: 'deliver'; access: Access } | { action: 'drop' }
function gate(ctx: Context): GateResult {
  const access = loadAccess()
  if (access.dmPolicy === 'disabled') return { action: 'drop' }
  const from = ctx.from
  if (!from) return { action: 'drop' }
  const senderId = String(from.id)
  const chatType = ctx.chat?.type
  if (chatType === 'private') {
    if (access.allowFrom.includes(senderId)) return { action: 'deliver', access }
    return { action: 'drop' }
  }
  if (chatType === 'group' || chatType === 'supergroup') {
    const groupId = String(ctx.chat!.id)
    // 导演模式：开关开 → 群消息全 drop（bot 不自己判断该不该回，只被 director.py 注入的 [director]
    // inbox 驱动）。真人消息已在 gate 之前 writeGroupTranscript（供导演读），故 drop 不丢事件。
    if (directorModeOn(groupId)) return { action: 'drop' }
    const policy = access.groups[groupId]
    if (!policy) return { action: 'drop' }
    const groupAllowFrom = policy.allowFrom ?? []
    // Bot senders matching otherBotUsernames bypass allowFrom (their ID needn't be whitelisted).
    const fromUsername = from.username ? `@${from.username}` : ''
    const isOtherBot = from.is_bot === true && fromUsername.length > 0 &&
      (policy.otherBotUsernames ?? []).some(u => u.toLowerCase() === fromUsername.toLowerCase())
    if (!isOtherBot && groupAllowFrom.length > 0 && !groupAllowFrom.includes(senderId)) return { action: 'drop' }
    const hasRouterCfg =
      (policy.selfAliases?.length ?? 0) > 0 ||
      (policy.otherBotUsernames?.length ?? 0) > 0 ||
      (policy.otherBotAliases?.length ?? 0) > 0
    if (hasRouterCfg) {
      if (decideGroupReply(ctx, policy, access.mentionPatterns) === 'drop') return { action: 'drop' }
      return { action: 'deliver', access }
    }
    const requireMention = policy.requireMention ?? true
    if (requireMention && !isMentioned(ctx, access.mentionPatterns)) return { action: 'drop' }
    return { action: 'deliver', access }
  }
  return { action: 'drop' }
}

// ─── worker 管理（跨平台，见 worker-manager.ts）────────────────────────
// unified session：群+私聊同一个 worker 会话，uuid5(ns, "unified")。
// 命名空间表在 worker-manager.ts 的 BOT_NAMESPACES（与 chat_history.py 必须一致）。
function sessionUuid(): string {
  return unifiedSessionUuid(BOT_NAME)
}

// ─── pause 检查 ───────────────────────────────────────────────────────
// 与 claudebotlife/pause.py 同路径：全局 ~/.claudebotlife.pause / 单 bot
// ~/.claudebotlife.pause-<bot>（跨平台：不再用 /tmp，py/ts 两侧一致）。
// pause 时连 DM 也不回。
function isPaused(): string | null {
  if (existsSync(join(homedir(), '.claudebotlife.pause'))) return 'global'
  if (existsSync(join(homedir(), `.claudebotlife.pause-${BOT_NAME}`))) return `bot:${BOT_NAME}`
  return null
}

// ─── worker spawn（经 worker-manager，headless 子进程，无 tmux）────────
function ensureWorkerRunning(_chatId: string): { uuid: string; spawned: boolean } {
  const mgr = getManager()
  const spawned = !mgr.isAlive()
  void mgr.ensure()
  return { uuid: mgr.sessionUuid, spawned }
}

// ─── attachment download (post-gate) ──────────────────────────────────
async function downloadToMedia(fileId: string, chatType: string | undefined): Promise<string | undefined> {
  try {
    const file = await bot.api.getFile(fileId)
    if (!file.file_path) return undefined
    const url = `https://api.telegram.org/file/bot${TOKEN}/${file.file_path}`
    const res = await fetch(url)
    if (!res.ok) return undefined
    const buf = Buffer.from(await res.arrayBuffer())
    const rawExt = file.file_path.includes('.') ? file.file_path.split('.').pop()! : 'bin'
    const ext = rawExt.replace(/[^a-zA-Z0-9]/g, '') || 'bin'
    const dir = (chatType === 'group' || chatType === 'supergroup') ? MEDIA_DIR_GROUP : MEDIA_DIR_DM
    mkdirSync(dir, { recursive: true })
    const path = join(dir, `${Date.now()}-${(file.file_unique_id ?? 'dl').replace(/[^a-zA-Z0-9_-]/g, '')}.${ext}`)
    writeFileSync(path, buf)
    return path
  } catch (e) {
    process.stderr.write(`dispatcher: download failed: ${e}\n`)
    return undefined
  }
}

// ─── inbox write ──────────────────────────────────────────────────────
let __inboxFallbackSeq = 0
function writeInbox(chatId: string, messageId: number | string, meta: any): void {
  // unified inbox：所有 chat 写同一个 <channel>/inbox/。文件名带 chatId 前缀，避免
  // 不同 chat 的 message_id 数值相同时相互覆盖（群/私聊现共用一个 inbox 目录）。
  const dir = join(CHANNEL_DIR, 'inbox')
  mkdirSync(dir, { recursive: true })
  const key = `${chatId}_${String(messageId)}`.replace(/[^a-zA-Z0-9_-]/g, '_')
  const tmp = join(dir, `${key}.json.tmp`)
  const fin = join(dir, `${key}.json`)
  writeFileSync(tmp, JSON.stringify(meta, null, 2))
  renameSync(tmp, fin)
}

// ─── group transcript (post-gate) ─────────────────────────────────────
function writeGroupTranscript(ctx: Context, text: string, imagePath?: string, attachment?: { kind: string; file_id: string }): void {
  const chatType = ctx.chat?.type
  if (chatType !== 'group' && chatType !== 'supergroup') return
  try {
    const tpath = join(GROUP_TRANSCRIPTS_DIR, `${ctx.chat!.id}.jsonl`)
    const line = JSON.stringify({
      ts: new Date((ctx.message?.date ?? Date.now() / 1000) * 1000).toISOString(),
      chat_id: String(ctx.chat!.id),
      message_id: ctx.message?.message_id,
      from_id: String(ctx.from?.id ?? ''),
      from_username: ctx.from?.username ?? null,
      is_bot: ctx.from?.is_bot === true,
      text: (text ?? '').slice(0, 1000),
      observed_by: botUsername,
      // 引用回复：真人在群里"回复某句话"时，记下被引用消息的文本+说话人，让导演/bot 知道
      // "用户这句是针对哪句说的"——据此按内容选谁接（不一定是被引用者本人）。
      ...(ctx.message?.reply_to_message ? {
        reply_to_text: (ctx.message.reply_to_message.text ?? ctx.message.reply_to_message.caption ?? '').slice(0, 200),
        reply_to_from: ctx.message.reply_to_message.from?.username ?? null,
      } : {}),
      ...(imagePath ? { image_path: imagePath } : {}),
      ...(attachment ? { attachment_kind: attachment.kind, attachment_file_id: attachment.file_id } : {}),
    }) + '\n'
    appendFileSync(tpath, line)
    try {
      const st = statSync(tpath)
      if (st.size > 5 * 1024 * 1024) {
        const all = readFileSync(tpath, 'utf8').split('\n')
        const kept = all.slice(Math.max(0, all.length - 2000)).join('\n')
        const tmp = tpath + '.tmp'
        writeFileSync(tmp, kept)
        renameSync(tmp, tpath)
      }
    } catch {}
  } catch {}
}

// ─── slash bridge intercept ───────────────────────────────────────────
// Match anything starting with `/word` — forwards every slash command to the
// worker (headless streaming 会话里 slash 当 user 消息喂，claude 原生处理
// /compact //clear //model 等；未知命令无功能危害)。Leading @mention
// is stripped so `@yourbot /compact` works in groups.
const SLASH_RE = /^\/[a-zA-Z][\w-]*(\s|$)/
async function slashBridgeIfApplicable(ctx: Context, text: string): Promise<boolean> {
  const trimmed = text.trim().replace(/^@\w+\s+/, '').trim()
  if (!SLASH_RE.test(trimmed)) return false
  process.stderr.write(`dispatcher[${BOT_NAME}]: slash-bridge fire chat=${ctx.chat?.id} cmd=${JSON.stringify(trimmed)}\n`)
  const chatType = ctx.chat?.type
  const isGroup = chatType === 'group' || chatType === 'supergroup'
  if (isGroup && !hasExplicitEntityMention(ctx)) return false
  const chatId = String(ctx.chat!.id)
  const msgId = ctx.message?.message_id
  try {
    getManager().sendSlash(trimmed)  // 排队走 stdin，manager 自己等 ready，无竞态
    await bot.api.sendMessage(chatId, `已下发: ${trimmed}`,
      msgId != null ? { reply_parameters: { message_id: msgId } } : {})
  } catch (e) {
    await bot.api.sendMessage(chatId, `[slash bridge 失败: ${String(e).slice(0, 200)}]`,
      msgId != null ? { reply_parameters: { message_id: msgId } } : {}).catch(() => {})
  }
  return true
}

// ─── inbound handler ──────────────────────────────────────────────────
type AttachmentMeta = { kind: string; file_id: string; size?: number; mime?: string; name?: string }

async function handleInbound(
  ctx: Context,
  text: string,
  imageSource?: (() => Promise<string | undefined>) | string,
  attachment?: AttachmentMeta,
  voiceText?: string,
  options?: { synthetic?: boolean },
): Promise<void> {
  const chatType = ctx.chat?.type
  const isGroup = chatType === 'group' || chatType === 'supergroup'
  const isBotSender = ctx.from?.is_bot === true
  const groupChatId = isGroup && ctx.chat ? String(ctx.chat.id) : undefined
  process.stderr.write(`dispatcher[${BOT_NAME}]: inbound${options?.synthetic ? '[synthetic]' : ''} chat=${ctx.chat?.id} from=${ctx.from?.id}(@${ctx.from?.username ?? '-'}, bot=${isBotSender}) type=${chatType} text=${JSON.stringify(text.slice(0, 80))}\n`)

  // 去重：bot-sender 消息可能通过 Telegram polling 和 peer_inbound 双路径到达
  if (isGroup && isBotSender) {
    const dupeMsgId = ctx.message?.message_id
    if (dupeMsgId != null && __isDupeAndMark(String(ctx.chat!.id), String(ctx.from!.id), String(dupeMsgId))) {
      process.stderr.write(`dispatcher[${BOT_NAME}]: dedup skip${options?.synthetic ? '[synthetic]' : ''} chat=${ctx.chat?.id} msgId=${dupeMsgId}\n`)
      return
    }
  }

  // 0a. Pause 拦截：pause 时 bot 全面闭嘴（含 DM），不 spawn worker / 不写 inbox。
  //     control 命令(/clearall)也一并 drop——pause 状态下本就无 worker 可清。
  const paused = isPaused()
  if (paused) {
    process.stderr.write(`dispatcher[${BOT_NAME}]: paused(${paused}) → drop inbound chat=${ctx.chat?.id}\n`)
    return
  }

  // 0. Allowlist prefilter (split out of gate) — runs before transcript & state
  //    machine so non-whitelisted strangers can't poison transcript or trip
  //    HARD_STOP/CONTINUE patterns. Bot senders matching otherBotUsernames bypass.
  if (isGroup && groupChatId) {
    const preAccess = loadAccess()
    const prePolicy = preAccess.groups[groupChatId]
    if (!prePolicy) return
    const preAllowFrom = prePolicy.allowFrom ?? []
    const preFromUsername = ctx.from?.username ? `@${ctx.from.username}` : ''
    const preIsOtherBot = ctx.from?.is_bot === true && preFromUsername.length > 0 &&
      (prePolicy.otherBotUsernames ?? []).some(u => u.toLowerCase() === preFromUsername.toLowerCase())
    if (!preIsOtherBot && preAllowFrom.length > 0 &&
        !preAllowFrom.includes(String(ctx.from?.id ?? ''))) return
  }

  // 1a. Group-wide /clearall: any whitelisted human (allowlist prefilter above
  //     has guaranteed this) can kill this bot's group worker. Each dispatcher
  //     handles its own worker independently — broadcast effect comes from
  //     Telegram delivering the same msg to all bots in the group.
  //     Skip synthetic peer-injected msgs and bot senders to avoid loops.
  if (isGroup && !isBotSender && !options?.synthetic && groupChatId &&
      /^\/clearall(@\w+)?$/.test(text.trim())) {
    // unified：只有一个 worker（群+私聊同脑）。/clearall 杀 worker（jsonl 保留，下次 resume）。
    const mgr = getManager()
    const killed = mgr.isAlive()
    mgr.kill({ intentional: true })
    process.stderr.write(`dispatcher[${BOT_NAME}]: /clearall chat=${groupChatId} killed=${killed}\n`)
    const msgId = ctx.message?.message_id
    await bot.api.sendMessage(groupChatId, `[${BOT_NAME}] ${killed ? '已清空本群记忆' : '本群无活跃 worker'}`,
      msgId != null ? { reply_parameters: { message_id: msgId } } : {}).catch(() => {})
    return
  }

  // 1. Human state machine (hard-stop / continue / budget reset) — runs BEFORE
  //    gate so even if gate later drops, user "闭嘴" etc. still takes effect.
  if (isGroup && !isBotSender && groupChatId) {
    onHumanGroupMsg(groupChatId, text)
  }

  // 2. Observe group msg → transcript（真人 + bot 都在 gate 之前落盘）。
  //    导演模式下 gate 会 drop 群里所有消息；若像原来那样把 bot-sender 的 transcript
  //    延后到 budget 通过之后（gate 之后）才写，bot 发言永远进不了 transcript，
  //    director.py 数不到 bot 发言 → heat 永远满 → 死循环硬闸失效。
  //    transcript 只是"群里说了什么"的客观记录（按 message_id 跨 bot 去重），与本 bot
  //    要不要路由无关；writeGroupTranscript 内部已对私聊 no-op。
  writeGroupTranscript(ctx, text, undefined,
    attachment ? { kind: attachment.kind, file_id: attachment.file_id } : undefined)

  // 2b. 已读回执：给真人消息加 👀 reaction。放在 gate 之前，即使 drop / bot 沉默不回也照加——
  //     让用户看到"已读了、只是没搭理"（私聊无原生双√，用 👀 代替）。**私聊只给白名单打**，
  //     否则会向陌生人确认"这号活着、有人在读"(隐私泄露 + 多余调用)；群里陌生人已在步骤0拦掉。
  if (!isBotSender && !options?.synthetic && ctx.message?.message_id != null) {
    const ackOk = chatType === 'private'
      ? loadAccess().allowFrom.includes(String(ctx.from?.id ?? ''))
      : true
    if (ackOk) {
      void bot.api.setMessageReaction(String(ctx.chat!.id), ctx.message.message_id,
        [{ type: 'emoji', emoji: '👀' as ReactionTypeEmoji['emoji'] }]).catch(() => {})
    }
  }

  // 3. Gate
  const result = gate(ctx)
  if (result.action === 'drop') { process.stderr.write(`dispatcher[${BOT_NAME}]: gate DROP chat=${ctx.chat?.id} from=${ctx.from?.id}\n`); return }
  const access = result.access

  // 4. Bot-sender budget check (after alias filter in gate, before routing)
  //    transcript 已在步骤 2（gate 之前）落盘，这里只决定"要不要路由给本 worker"。
  if (isGroup && isBotSender && groupChatId) {
    if (!allowBotSenderRoute(groupChatId)) {
      process.stderr.write(`dispatcher[${BOT_NAME}]: bot-sender dropped by budget/lock on chat ${groupChatId}\n`)
      return
    }
  }

  // 5. Slash bridge before routing (skip for synthetic peer-injected msgs —
  //    they're not real user input, shouldn't forward to TUI as /commands)
  if (!options?.synthetic && await slashBridgeIfApplicable(ctx, text)) return

  const from = ctx.from!
  const chatId = String(ctx.chat!.id)
  const msgId = ctx.message?.message_id

  // Skip UI affordances for synthetic peer-injected msgs (no real Telegram msg
  // to react to, and typing indicator is for human→bot direction only).
  if (!options?.synthetic) {
    void bot.api.sendChatAction(chatId, 'typing').catch(() => {})
    if (access.ackReaction && msgId != null) {
      void bot.api.setMessageReaction(chatId, msgId, [
        { type: 'emoji', emoji: access.ackReaction as ReactionTypeEmoji['emoji'] },
      ]).catch(() => {})
    }
  }

  const imagePath = typeof imageSource === 'function' ? await imageSource() : imageSource

  // ensure worker
  ensureWorkerRunning(chatId)

  const meta: Record<string, unknown> = {
    chat_id: chatId,
    scene: isGroup ? 'group' : 'private',   // unified worker 靠这个标注群/私聊来源
    message_id: msgId != null ? String(msgId) : undefined,
    from_id: String(from.id),
    from_username: from.username ?? null,
    sender_username: from.username,
    chat_type: ctx.chat?.type ?? 'private',
    is_bot_sender: from.is_bot === true,
    text,
    ts: new Date((ctx.message?.date ?? 0) * 1000).toISOString(),
    reply_to: ctx.message?.reply_to_message?.message_id,
  }
  if (imagePath) meta.image_path = imagePath
  if (voiceText) meta.voice_text = voiceText
  if (attachment) {
    meta.attachment_kind = attachment.kind
    meta.attachment_file_id = attachment.file_id
    if (attachment.size != null) meta.attachment_size = attachment.size
    if (attachment.mime) meta.attachment_mime = attachment.mime
    if (attachment.name) meta.attachment_name = attachment.name
  }
  // crossmem 已作废：unified session 天然跨场景（群+私聊同一会话、记忆相通），
  // 不再需要把"另一个场景"的尾巴当背景注入。此处不做任何注入。
  const inboxKey = msgId != null ? String(msgId) : `t${Date.now()}-${++__inboxFallbackSeq}`
  writeInbox(chatId, inboxKey, meta)
  process.stderr.write(`dispatcher[${BOT_NAME}]: routed chat=${chatId} inboxKey=${inboxKey} synthetic=${!!options?.synthetic}\n`)

  // ─── 写 last-user marker（给 self-initiate / chat_history 算 since_last_user_min 用） ───
  // 仅当真人私聊（非 bot sender、非 synthetic peer 注入）时才更新。否则 since 会
  // 被 peer-inbound / synthetic 重置为 0，self-initiate 永远进入"非静默期"被拦下。
  if (!meta.is_bot_sender && !options?.synthetic && (meta.chat_type === 'private' || ctx.chat?.type === 'private')) {
    try {
      const stateDir = join(homedir(), '.claude', 'dispatcher', '.self-initiate-state')
      mkdirSync(stateDir, { recursive: true })
      const markerPath = join(stateDir, `${BOT_NAME}-${chatId}.last-user`)
      const ts = Math.floor((ctx.message?.date ?? Date.now() / 1000))
      writeFileSync(markerPath, String(ts))
    } catch (e) {
      process.stderr.write(`dispatcher[${BOT_NAME}]: last-user marker write failed: ${e}\n`)
    }
  }
}

function safeName(s: string | undefined): string | undefined {
  return s?.replace(/[<>\[\]\r\n;]/g, '_')
}

// ─── grammY handlers ──────────────────────────────────────────────────
bot.on('message:text', ctx => handleInbound(ctx, ctx.message.text))
bot.on('message:photo', ctx => {
  const caption = ctx.message.caption ?? '(photo)'
  return handleInbound(ctx, caption, async () => {
    const photos = ctx.message.photo
    const best = photos[photos.length - 1]
    return downloadToMedia(best.file_id, ctx.chat?.type)
  })
})
bot.on('message:document', ctx => {
  const d = ctx.message.document
  const name = safeName(d.file_name)
  const text = ctx.message.caption ?? `(document: ${name ?? 'file'})`
  return handleInbound(ctx, text, undefined, { kind: 'document', file_id: d.file_id, size: d.file_size, mime: d.mime_type, name })
})
bot.on('message:voice', async ctx => {
  const voice = ctx.message.voice
  let text = ctx.message.caption ?? '(voice message)'
  let voiceText: string | undefined
  try {
    const r = await fetch('http://127.0.0.1:7788/transcribe_telegram', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: voice.file_id, bot_token: bot.token }),
    })
    if (r.ok) {
      const j = await r.json() as { text?: string }
      if (j.text && j.text.trim()) { text = j.text.trim(); voiceText = text }
    }
  } catch {}
  return handleInbound(ctx, text, undefined,
    { kind: 'voice', file_id: voice.file_id, size: voice.file_size, mime: voice.mime_type },
    voiceText)
})
bot.on('message:audio', ctx => {
  const a = ctx.message.audio
  const text = ctx.message.caption ?? `(audio: ${safeName(a.title) ?? safeName(a.file_name) ?? 'audio'})`
  return handleInbound(ctx, text, undefined, { kind: 'audio', file_id: a.file_id, size: a.file_size, mime: a.mime_type, name: safeName(a.file_name) })
})
bot.on('message:video', ctx => {
  const v = ctx.message.video
  return handleInbound(ctx, ctx.message.caption ?? '(video)', undefined,
    { kind: 'video', file_id: v.file_id, size: v.file_size, mime: v.mime_type, name: safeName(v.file_name) })
})
bot.on('message:video_note', ctx => {
  const vn = ctx.message.video_note
  return handleInbound(ctx, '(video note)', undefined, { kind: 'video_note', file_id: vn.file_id, size: vn.file_size })
})
bot.on('message:sticker', ctx => {
  const s = ctx.message.sticker
  return handleInbound(ctx, `(sticker${s.emoji ? ' ' + s.emoji : ''})`, undefined,
    { kind: 'sticker', file_id: s.file_id, size: s.file_size })
})

bot.catch(err => process.stderr.write(`dispatcher: handler error: ${err.error}\n`))

// ─── text chunking (for /send) ───────────────────────────────────────
const MAX_CHUNK_LIMIT = 4096
function chunkText(text: string, limit: number, mode: 'length' | 'newline'): string[] {
  if (text.length <= limit) return [text]
  const out: string[] = []
  let rest = text
  while (rest.length > limit) {
    let cut = limit
    if (mode === 'newline') {
      const para = rest.lastIndexOf('\n\n', limit)
      const line = rest.lastIndexOf('\n', limit)
      const space = rest.lastIndexOf(' ', limit)
      cut = para > limit / 2 ? para : line > limit / 2 ? line : space > 0 ? space : limit
    }
    out.push(rest.slice(0, cut))
    rest = rest.slice(cut).replace(/^\n+/, '')
  }
  if (rest) out.push(rest)
  return out
}
const PHOTO_EXTS = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp'])

// ─── HTTP server for workers ─────────────────────────────────────────
const server = Bun.serve({
  port: DISPATCHER_PORT,
  hostname: '127.0.0.1',
  async fetch(req) {
    const url = new URL(req.url)
    try {
      if (req.method !== 'POST') return new Response('method not allowed', { status: 405 })

      // ─── worker 管理端点（免 body；跨进程查活/状态，替代 tmux has-session/ls）────
      if (url.pathname === '/ensure_worker') {
        const mgr = getManager()
        const running = mgr.isAlive()
        void mgr.ensure()
        return Response.json({ ok: true, running, spawned: !running, uuid: mgr.sessionUuid })
      }
      if (url.pathname === '/status') {
        return Response.json({ ok: true, ...getManager().status() })
      }

      const body = await req.json() as any

      if (url.pathname === '/send') {
        const chatId = String(body.chat_id)
        const text = String(body.text ?? '')
        const replyToNum = body.reply_to != null ? Number(body.reply_to) : undefined
        const replyTo = (replyToNum != null && Number.isFinite(replyToNum)) ? replyToNum : undefined
        let files: string[] = []
        if (Array.isArray(body.files)) files = body.files
        else if (typeof body.files === 'string' && body.files.trim()) {
          try { const p = JSON.parse(body.files); if (Array.isArray(p)) files = p }
          catch { files = [body.files] }
        }
        const asVoice = body.as_voice === true || body.as_voice === 'true'
        const voiceEmotion = body.voice_emotion != null ? String(body.voice_emotion) : 'NEUTRAL'
        const voiceInstruct = body.voice_instruct != null ? String(body.voice_instruct) : ''
        // 如果 worker 没传 voice_text 但 as_voice=true，自动从 text 剥掉 @username 作为
        // TTS 源（气泡 text 保留 @ 才能触发对方 bot；语音不念 @xxx_bot 才自然）
        const stripMentions = (s: string) => s.replace(/@[A-Za-z0-9_]+/g, '').replace(/[ \t]{2,}/g, ' ').replace(/[ \t]+\n/g, '\n').trim()
        const voiceTextRaw = body.voice_text != null
          ? String(body.voice_text)
          : ((body.as_voice === true || body.as_voice === 'true') ? stripMentions(String(body.text ?? '')) : '')
        const access = loadAccess()
        const limit = Math.max(1, Math.min(access.textChunkLimit ?? MAX_CHUNK_LIMIT, MAX_CHUNK_LIMIT))
        const mode = access.chunkMode ?? 'length'
        const replyMode = access.replyToMode ?? 'first'
        const paragraphs = access.splitOnParagraph
          ? text.split('\n\n').map(s => s.trim()).filter(s => s.length > 0)
          : [text]
        const allChunks: string[] = []
        for (const p of paragraphs) allChunks.push(...chunkText(p, limit, mode))
        const voiceChunks: string[] = asVoice && voiceTextRaw
          ? voiceTextRaw.split('\n\n').map(s => s.trim())
          : []
        const sentIds: number[] = []
        const delay = access.splitOnParagraph ? (access.paragraphDelay ?? 0) : 0

        // 每段发送内部重试：代理(7897)偶发网络抖动会让 sendMessage 抛错，若不吸收，
        // 整条 /send 就 500 → worker 以为整条失败 → 重发整条 → 已送达的段重复(实测 bug)。
        // 网络类失败通常意味着"没发出去"，重试同段是安全的；重试掉的多是瞬时抖动。
        const sendChunk = async (text: string, opts: any): Promise<{ message_id: number }> => {
          let lastErr: unknown
          for (let a = 1; a <= 3; a++) {
            try { return await bot.api.sendMessage(chatId, text, opts) }
            catch (e) {
              lastErr = e
              process.stderr.write(`dispatcher[${BOT_NAME}]: sendMessage 第${a}次失败(${String(e).slice(0,80)})${a<3?'，重试':'，放弃'}\n`)
              if (a < 3) await new Promise(r => setTimeout(r, 400 * a))
            }
          }
          throw lastErr
        }

        for (let i = 0; i < allChunks.length; i++) {
          if (i > 0 && delay > 0) {
            void bot.api.sendChatAction(chatId, 'typing').catch(() => {})
            await new Promise(r => setTimeout(r, delay))
          }
          const shouldReplyTo = replyTo != null && replyMode !== 'off' && (replyMode === 'all' || i === 0)
          if (asVoice) {
            const voiceId = access.voiceId
            if (!voiceId) throw new Error('as_voice=true but access.json missing voiceId')
            const hasDual = voiceChunks.length > 0
            const ttsText = hasDual ? (voiceChunks[i] ?? '') : allChunks[i]
            const skipVoice = !ttsText || ttsText.trim() === ''
            if (hasDual) {
              const sent = await sendChunk(allChunks[i],
                shouldReplyTo ? { reply_parameters: { message_id: replyTo! } } : {})
              sentIds.push(sent.message_id)
            }
            if (!skipVoice) {
              const vResp = await fetch('http://127.0.0.1:7788/send_voice', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  bot_token: bot.token,
                  chat_id: chatId,
                  text: ttsText,
                  voice_id: voiceId,
                  emotion: voiceEmotion,
                  instruct: voiceInstruct,
                  reply_to_message_id: (shouldReplyTo && !hasDual) ? replyTo : undefined,
                }),
              })
              if (!vResp.ok) throw new Error(`voice-bridge ${vResp.status}: ${await vResp.text().catch(() => '')}`)
              const { message_id } = await vResp.json() as { message_id: number }
              sentIds.push(message_id)
            }
          } else {
            const sent = await sendChunk(allChunks[i],
              shouldReplyTo ? { reply_parameters: { message_id: replyTo! } } : {})
            sentIds.push(sent.message_id)
          }
        }

        for (const f of files) {
          const ext = extname(f).toLowerCase()
          const kind = PHOTO_EXTS.has(ext) ? 'photo' : 'document'
          const replyToId = (replyTo != null && replyMode !== 'off') ? replyTo : undefined
          const fResp = await fetch('http://127.0.0.1:7788/send_file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bot_token: bot.token, chat_id: chatId, file_path: f, kind, reply_to_message_id: replyToId }),
          })
          if (!fResp.ok) throw new Error(`voice-bridge send_file ${fResp.status}: ${await fResp.text().catch(() => '')}`)
          const { message_id } = await fResp.json() as { message_id: number }
          sentIds.push(message_id)
        }
        // Peer inject (groups only; fire-and-forget). Bypass Telegram's
        // bot-to-bot invisibility by notifying peer dispatchers directly.
        // Peer 看到的"真实内容"优先用 voice_text（as_voice 时 text 是播报稿/占位符，
        // voice_text 才是 TTS 合成前的原文）。否则用 text。
        const peerText = (asVoice && voiceTextRaw) ? voiceTextRaw : text
        // Peer 互推：无论导演模式与否都推。原因——Telegram 平台不投递 bot→bot 消息，
        // peer_inbound 是 bot 发言进入 group_transcript 的唯一通道；director.py 靠 transcript
        // 里的 bot 发言算 heat、判断该谁接、给被选中 bot 提供群内上下文。导演模式下不推 =
        // transcript 永远只有真人消息 = director 对每条真人消息只 inject 一个 bot 就停（bot 回复
        // 不回流→没有新消息触发下一轮）＋被选中 bot 看不到别人说了啥（像失忆）。
        // 抢答风险：导演模式下 gate 会 drop 掉 synthetic peer 消息（不路由给 worker），所以 peer
        // 推送只喂 transcript、不会引发 bot 互相回复，安全。
        if (String(chatId).startsWith('-') && PEERS.length > 0 && peerText.trim().length > 0) {
          const payload = {
            source_bot: BOT_NAME,
            source_username: botUsername,
            source_user_id: sourceBotUserId,
            chat_id: chatId,
            chat_type: 'supergroup',
            text: peerText,
            message_ids: sentIds,
            reply_to: replyTo,
            files,
          }
          for (const peer of PEERS) {
            fetch(`${peer.url}/peer_inbound`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            }).catch(e => process.stderr.write(
              `dispatcher[${BOT_NAME}]: peer inject ${peer.name} failed: ${e}\n`))
          }
        }
        return Response.json({ ok: true, message_ids: sentIds })
      }

      if (url.pathname === '/react') {
        const chatId = String(body.chat_id)
        const messageId = Number(body.message_id)
        const emoji = String(body.emoji) as ReactionTypeEmoji['emoji']
        await bot.api.setMessageReaction(chatId, messageId, [{ type: 'emoji', emoji }])
        return Response.json({ ok: true })
      }

      if (url.pathname === '/download') {
        const fileId = String(body.file_id)
        let chatType = body.chat_type as string | undefined
        if (!chatType && body.chat_id != null) {
          // Telegram convention: negative chat_id = group/supergroup, positive = private
          chatType = String(body.chat_id).startsWith('-') ? 'supergroup' : 'private'
        }
        const path = await downloadToMedia(fileId, chatType)
        if (!path) return new Response('download failed', { status: 500 })
        return Response.json({ ok: true, path })
      }

      if (url.pathname === '/add_group_alias') {
        const groupId = String(body.group_id)
        const aliasRaw = String(body.alias ?? '').trim()
        const kind = String(body.kind)
        if (!aliasRaw || aliasRaw.length > 20) return new Response('bad alias', { status: 400 })
        if (kind !== 'self' && kind !== 'other') return new Response('bad kind', { status: 400 })
        if (!/^-?\d+$/.test(groupId)) return new Response('bad group_id', { status: 400 })
        const raw = readFileSync(ACCESS_FILE, 'utf8')
        const parsed = JSON.parse(raw)
        parsed.groups = parsed.groups ?? {}
        parsed.groups[groupId] = parsed.groups[groupId] ?? { requireMention: false, allowFrom: [] }
        const g = parsed.groups[groupId]
        const field = kind === 'self' ? 'selfAliases' : 'otherBotAliases'
        const arr: string[] = Array.isArray(g[field]) ? g[field] : []
        if (!arr.includes(aliasRaw)) arr.push(aliasRaw)
        g[field] = arr
        const tmp = ACCESS_FILE + '.tmp'
        writeFileSync(tmp, JSON.stringify(parsed, null, 2) + '\n', { mode: 0o600 })
        renameSync(tmp, ACCESS_FILE)
        return Response.json({ ok: true, field, alias: aliasRaw, total: arr.length })
      }

      if (url.pathname === '/inject') {
        // 调试端点：手动注入任意文本到 worker stdin（替代 tmux attach 打字）。仅本机可达。
        const text = String(body.text ?? '').trim()
        if (!text) return new Response('text required', { status: 400 })
        getManager().injectRaw(text)
        return Response.json({ ok: true })
      }

      if (url.pathname === '/peer_inbound') {
        const b = body
        const chatIdStr = String(b.chat_id ?? '')
        const syntheticCtx = {
          chat: { id: Number(b.chat_id), type: b.chat_type ?? 'supergroup' },
          from: {
            id: Number(b.source_user_id ?? 0),
            is_bot: true,
            username: typeof b.source_username === 'string' ? b.source_username : undefined,
          },
          message: {
            message_id: Array.isArray(b.message_ids) && b.message_ids.length > 0 ? Number(b.message_ids[0]) : undefined,
            text: String(b.text ?? ''),
            date: Math.floor(Date.now() / 1000),
            reply_to_message: b.reply_to != null ? { message_id: Number(b.reply_to) } : undefined,
            entities: [],
          },
        } as unknown as Context
        const firstImage = Array.isArray(b.files)
          ? b.files.find((f: any) => typeof f === 'string' && /\.(png|jpe?g|webp|gif)$/i.test(f))
          : undefined
        process.stderr.write(`dispatcher[${BOT_NAME}]: peer_inbound from=${b.source_bot} chat=${chatIdStr} text=${JSON.stringify(String(b.text ?? '').slice(0, 60))}\n`)
        await handleInbound(syntheticCtx, String(b.text ?? ''), firstImage, undefined, undefined, { synthetic: true })
        return Response.json({ ok: true })
      }

      return new Response('not found', { status: 404 })
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      process.stderr.write(`dispatcher HTTP: ${msg}\n`)
      return new Response(msg, { status: 500 })
    }
  },
})
process.stderr.write(`dispatcher[${BOT_NAME}]: HTTP listening on 127.0.0.1:${DISPATCHER_PORT}\n`)

// ─── start polling (with 409 retry) ───────────────────────────────────
void (async () => {
  for (let attempt = 1; ; attempt++) {
    try {
      await bot.start({
        onStart: info => {
          botUsername = info.username
          sourceBotUserId = info.id
          process.stderr.write(`dispatcher[${BOT_NAME}]: polling as @${info.username} (id=${info.id}, peers=${PEERS.length})\n`)
          // Peer config 自检：每个 peer 的 @username 必须在至少一个 group 的 otherBotUsernames 里，
          // 否则该 peer 发来的 /peer_inbound 会被 prefilter 静默 drop。
          try {
            const acc = loadAccess()
            const allOtherBotUsernames = new Set<string>()
            for (const g of Object.values(acc.groups ?? {})) {
              for (const u of g.otherBotUsernames ?? []) allOtherBotUsernames.add(u.toLowerCase())
            }
            for (const p of PEERS) {
              const tag = `@${p.username}`.toLowerCase()
              if (!allOtherBotUsernames.has(tag)) {
                process.stderr.write(`dispatcher[${BOT_NAME}]: ⚠ peer ${p.name} (${tag}) NOT in any group's otherBotUsernames — peer_inbound from it will be DROPPED\n`)
              }
            }
          } catch (e) { process.stderr.write(`dispatcher[${BOT_NAME}]: peer self-check failed: ${e}\n`) }
        },
      })
      return
    } catch (err) {
      if (err instanceof GrammyError && err.error_code === 409) {
        const delay = Math.min(1000 * attempt, 15000)
        process.stderr.write(`dispatcher[${BOT_NAME}]: 409, retry in ${delay}ms\n`)
        await new Promise(r => setTimeout(r, delay))
        continue
      }
      if (err instanceof Error && err.message === 'Aborted delay') return
      process.stderr.write(`dispatcher[${BOT_NAME}]: polling failed: ${err}\n`)
      return
    }
  }
})()

function shutdown() {
  process.stderr.write(`dispatcher[${BOT_NAME}]: shutting down\n`)
  setTimeout(() => process.exit(0), 2000)
  void Promise.resolve(bot.stop()).finally(() => process.exit(0))
}
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
