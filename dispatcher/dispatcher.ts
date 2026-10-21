#!/usr/bin/env bun
/**
 * Per-bot Telegram dispatcher — grammY long-poll + HTTP server.
 *
 * Runs one process per bot (e.g. mybot). Owns the Telegram bot token,
 * performs gate+route, writes inbound messages to inbox files, and exposes
 * /send /react /download /add_group_alias /ensure_worker /status /inject.
 *
 * Worker (headless claude, stream-json) 由同进程的 worker-manager.ts 托管：
 * inbox → stdin 注入，崩溃自动 --resume，跨平台（macOS/Windows/Linux 无 tmux）。
 */

import { Bot, GrammyError, type Context } from 'grammy'
import type { ReactionTypeEmoji } from 'grammy/types'
import {
  readFileSync, writeFileSync, mkdirSync, statSync, renameSync, appendFileSync, existsSync, chmodSync,
  readdirSync, rmSync, mkdtempSync, openSync, readSync, closeSync,
} from 'fs'
import { spawn } from 'child_process'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { join, extname, basename, dirname } from 'path'
import { homedir, tmpdir } from 'os'
import { getManager, unifiedSessionUuid, projectSlug, resolveClaude, killTree } from './worker-manager.ts'
import { winCmdSpawnSpec } from './win_cmd'
import { buildSendPlan } from './send_plan'
import { classifySendError, describeSendError } from './send_error'
import { startProviderWatch } from './provider_watch'
import { createPromiseRuntime } from './promise_runtime'
import {
  makeAsleepProbe, makeBotDisabledCheck, makeInboxRetract, makeSessionScanner, makeStopCheck, markOutbound,
} from './promise_wire'
import { makePauseCheck } from './pause_gate'
import { sanitizePromiseIds } from './promise_plan'
import { readProviderFpStatus } from './provider_fp'
import { telegramApiBase, planFileDrops } from './feature_gate'
import { channelsMediaRoot, resolveAllowedFile } from './file_allowlist'
import { senderAllowed } from './inbound_access'
import { voiceBridgeAuthHeaders } from './voice_bridge_auth'
import { readFeatures, featureOn, type FeatureFlags } from './features'
import {
  deletePhotoState, isValidPhotoChatId, isPhotoExit, nextPhotoRound, nowMsFromEnv, parsePhotoTrigger,
  photoExpiresAtMs, photoMeta, photoModeRoot, photoStatePath, readPhotoState, triggerPhotoState, writePhotoState,
} from './photo_mode'
import {
  deletePartyState, inboundItemSendersAllOwner, isPartyEndPhrase, isPartyOptOutPrefix, isValidGroupId,
  newPartyState, normalizePartyChatId, normalizePartyNote, ownerOf, ownerWindowOpen, participatingPartyGroups,
  partyModeRoot, partyStatePath, partyStateResponse, readPartyState, refreshPartyState, writePartyState,
  type InboundSenderRecord,
} from './party_mode'
import {
  applyGroupMessage, applyMyChatMember, isoSeconds, minIntervalSecondsFromEnv,
  readGroupsSeenFile, withGroupsSeenLock, writeGroupsSeenFile, writeJsonAtomic,
} from './groups_seen'
import { parseBotCtl, botCtlNotice } from './botctl'
import {
  parseRecentTurns, summarizeTurns, runClearSummary, AUTH_FAILED,
  RECEIPT_CLEARED_SKIP, RECEIPT_NO_SUMMARY,
  type ClearSummaryResult,
} from './clear_summary'
import { createHangRuntime } from './hang_runtime'
import { sweepStaleSynthetic, STALE_SYNTHETIC_MS } from './inbox_names'
import { probeSituation } from './situation_bridge'
import { inboxHumanMarks } from './chat_guard'

// ─── env ──────────────────────────────────────────────────────────────
const CHANNEL_DIR = process.env.CHANNEL_DIR || ''
const TOKEN = process.env.TELEGRAM_BOT_TOKEN || ''
const DISPATCHER_PORT = Number(process.env.DISPATCHER_PORT || '17801')
const BOT_NAME = process.env.BOT_NAME || ''
// Bot API 根的测试接缝：只收回环地址（请求 URL 里带令牌）；未设时照旧走官方地址。
const TELEGRAM_API_BASE = telegramApiBase(process.env.DISPATCHER_TELEGRAM_API_BASE)
if (!TELEGRAM_API_BASE.ok) {
  process.stderr.write('dispatcher: DISPATCHER_TELEGRAM_API_BASE 不合规（只收 http://127.0.0.1:<端口>），拒绝启动\n')
  process.exit(1)
}
const TELEGRAM_FILE_ROOT = TELEGRAM_API_BASE.apiRoot ?? 'https://api.telegram.org'

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

/** H-15：向 peer dispatcher 注入 /peer_inbound 需带它的本机口令。
 *  口令按既有约定取自 peer 的通道目录 ``~/.claude/channels/<peer.name>/.api-token``；
 *  读不到 → None，调用方记一行日志并跳过该 peer，绝不无鉴权硬发。 */
function peerApiToken(peer: PeerConfig): string | null {
  const dir = process.env[`CHANNEL_DIR_${peer.name.toUpperCase()}`] || join(homedir(), '.claude', 'channels', peer.name)
  try { return readFileSync(join(dir, '.api-token'), 'utf8').trim() || null } catch { return null }
}

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
  hangEnabled?: boolean       // 被晾感知总开关，缺省开；false = 她永远不会主动追问
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
      hangEnabled: p.hangEnabled,
    }
  } catch {
    return { dmPolicy: 'disabled', allowFrom: [], groups: {}, pending: {} }
  }
}

// ─── 功能开关 ─────────────────────────────────────────────────────────
// 开关存在 <CHANNEL_DIR>/features.json，唯一写者是 Python 侧 bot_features；这里只读、
// 用到时现读（readFeatures 按修改时间缓存），改完下一条消息生效，不用重启调度器。
// 读不出按八项全关；这种情况每小时最多记一行日志，免得每条消息都刷一遍。
const FEATURES_UNREADABLE_LOG_MS = 3600_000
let featuresUnreadableLoggedAt = -Infinity
function botFeatures(): FeatureFlags {
  const r = readFeatures(CHANNEL_DIR)
  if (!r.readable) {
    const now = Date.now()
    if (now - featuresUnreadableLoggedAt >= FEATURES_UNREADABLE_LOG_MS) {
      featuresUnreadableLoggedAt = now
      process.stderr.write(`dispatcher[${BOT_NAME}]: features_unreadable 读不出 features.json，功能开关按全关处理\n`)
    }
  }
  return r.features
}
// 发往群而群聊关着（/send）、群聊关着时调 /add_group_alias：同一个 409 正文
const GROUP_FEATURE_OFF_BODY = { ok: false, code: 'feature_off', message: '群聊功能关着' }
// /send 出口兜底的两个目录：生图目录与表情库图片根
const IMAGE_OUTPUT_DIR = join(homedir(), 'resource', 'media', BOT_NAME)
function stickerImageRoot(): string {
  const raw = process.env.STICKERS_DIR || '~/resource/stickers'
  return raw === '~' || raw.startsWith('~/') ? join(homedir(), raw.slice(1)) : raw
}
// /send 的 files 只允许这些根目录（软链逃逸由 resolveAllowedFile 的 realpath 校验兜住）。
function allowedFileRoots(): string[] {
  const roots = [
    IMAGE_OUTPUT_DIR,
    stickerImageRoot(),
    join(CHANNEL_DIR, 'inbox'),
    join(CHANNEL_DIR, 'media'),
    // download_attachment 落在 <channels>/media/<bot>/telegram，是频道根的兄弟目录。
    channelsMediaRoot(process.env, homedir()),
  ]
  const moments = process.env.MOMENTS_MEDIA_ROOT
  if (moments) roots.push(moments)
  return roots
}

// ─── party-camera：状态目录与 owner 观测 ─────────────────────────────
// 拍照按 bot/chat 独立；聚会是群级共享（文件名不含 bot）。
const PHOTO_MODE_DIR = photoModeRoot(CHANNEL_DIR)
const PARTY_MODE_DIR = partyModeRoot()
// owner 判定只认 dispatcher 亲见的群消息（不信任 worker 自报）：预筛后记录本群最后一条
// 真人消息发送者与 owner 最近发言时刻；/party 要求两者都在 10 分钟窗口内。
const OWNER_GROUP_WINDOW_MS = 10 * 60_000
const lastHumanSenderId = new Map<string, string>()
const lastOwnerGroupMsgAtMs = new Map<string, number>()
// 已路由真人入站件 `${chatId}|${messageId}` → 发送者；/party 必须绑定当前投递件的 message_id。
const inboundHumanSenders = new Map<string, InboundSenderRecord>()
function rememberInboundHumanSender(chatId: string, messageId: string, senderId: string, atMs: number): void {
  inboundHumanSenders.set(`${chatId}|${messageId}`, { senderId, atMs })
  if (inboundHumanSenders.size > 500) {
    for (const [k, v] of inboundHumanSenders) if (atMs - v.atMs > OWNER_GROUP_WINDOW_MS) inboundHumanSenders.delete(k)
  }
}

// ─── 刚发现的群与 .telegram-me.json ───────────────────────────────────
// 两条记录路径（my_chat_member 与陌生群消息）都不看 groups 开关。
const GROUPS_SEEN_FILE = join(CHANNEL_DIR, 'groups-seen.json')
const TELEGRAM_ME_FILE = join(CHANNEL_DIR, '.telegram-me.json')

function logGroupsSeenError(what: string, e: unknown): void {
  process.stderr.write(`dispatcher[${BOT_NAME}]: groups_seen ${what}: ${e instanceof Error ? e.message : String(e)}\n`)
}

/** 群消息发现：陌生群建一条；已有记录只更新 title/last_seen（限频由 applyGroupMessage 判）。 */
function recordSeenGroupMessage(chat: { id: number | string; title?: string; type?: string }, createIfMissing: boolean): void {
  const interval = minIntervalSecondsFromEnv(process.env.DISPATCHER_GROUPS_SEEN_MIN_INTERVAL_S)
  try {
    withGroupsSeenLock(GROUPS_SEEN_FILE, () => {
      const { doc, error } = readGroupsSeenFile(GROUPS_SEEN_FILE)
      if (error) logGroupsSeenError('读不出，按空记录处理', error)
      const changed = applyGroupMessage(doc, { chatId: String(chat.id), title: chat.title, type: chat.type },
        Date.now(), interval, createIfMissing)
      if (changed) writeGroupsSeenFile(GROUPS_SEEN_FILE, doc)
    })
  } catch (e) {
    logGroupsSeenError('写入失败', e)
  }
}

/** my_chat_member：身份变化不受限频；added_by_owner 以私聊白名单为准。 */
function recordMyChatMember(ctx: Context): void {
  const upd = ctx.myChatMember
  const chat = upd?.chat as { id: number; type?: string; title?: string; migrate_to_chat_id?: number } | undefined
  if (!chat || (!chat.migrate_to_chat_id && chat.type !== 'group' && chat.type !== 'supergroup')) return
  try {
    withGroupsSeenLock(GROUPS_SEEN_FILE, () => {
      const { doc, error } = readGroupsSeenFile(GROUPS_SEEN_FILE)
      if (error) logGroupsSeenError('读不出，按空记录处理', error)
      applyMyChatMember(doc, {
        chatId: String(chat.id),
        title: chat.title,
        type: chat.type,
        status: upd!.new_chat_member?.status,
        actorId: upd!.from?.id,
        ownerIds: loadAccess().allowFrom.map(String),
        migrateTo: chat.migrate_to_chat_id,
      }, Date.now())
      writeGroupsSeenFile(GROUPS_SEEN_FILE, doc)
    })
  } catch (e) {
    logGroupsSeenError('写入失败', e)
  }
}

/** .telegram-me.json：只写这四个值与时间，不含令牌；写失败只记一行。 */
function writeTelegramMe(info: { id: number; username?: string; can_join_groups?: boolean; can_read_all_group_messages?: boolean }): void {
  try {
    writeJsonAtomic(TELEGRAM_ME_FILE, {
      username: typeof info.username === 'string' ? info.username : null,
      id: info.id,
      can_read_all: typeof info.can_read_all_group_messages === 'boolean' ? info.can_read_all_group_messages : null,
      can_join_groups: typeof info.can_join_groups === 'boolean' ? info.can_join_groups : null,
      at: isoSeconds(),
    })
  } catch (e) {
    process.stderr.write(`dispatcher[${BOT_NAME}]: telegram_me_write_failed: ${e instanceof Error ? e.message : String(e)}\n`)
  }
}

const bot = TELEGRAM_API_BASE.apiRoot ? new Bot(TOKEN, { client: { apiRoot: TELEGRAM_API_BASE.apiRoot } }) : new Bot(TOKEN)
let botUsername = ''
// 应用层健康信号：最近一次 getUpdates 成功的时间戳（/health 以秒暴露）。
let lastPollOk = Date.now()
bot.api.config.use((prev, method, payload, signal) =>
  prev(method, payload, signal).then(res => {
    if (method === 'getUpdates') lastPollOk = Date.now()
    return res
  }))

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
export function gate(ctx: Context): GateResult {
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
    // 这只 bot 的群聊关着 → 群消息一律不收（access.json 的 groups 原样留着，打开就恢复）
    if (!featureOn(botFeatures(), 'groups')) return { action: 'drop' }
    const groupId = String(ctx.chat!.id)
    // 导演模式：开关开 → 群消息全 drop（bot 不自己判断该不该回，只被 director.py 注入的 [director]
    // inbox 驱动）。真人消息已在 gate 之前 writeGroupTranscript（供导演读），故 drop 不丢事件。
    if (directorModeOn(groupId)) return { action: 'drop' }
    const policy = access.groups[groupId]
    if (!policy) return { action: 'drop' }
    // Bot senders matching otherBotUsernames bypass allowFrom (their ID needn't be whitelisted).
    const fromUsername = from.username ? `@${from.username}` : ''
    const isOtherBot = from.is_bot === true && fromUsername.length > 0 &&
      (policy.otherBotUsernames ?? []).some(u => u.toLowerCase() === fromUsername.toLowerCase())
    // 群放行统一走 senderAllowed（H-14 纯函数）：群 allowFrom 非空只认它；为空回退
    // 私聊 owner 名单；两者都空 → 拒绝。绝不把“空名单”当“所有人放行”。peer bot 仍豁免。
    if (!isOtherBot && !senderAllowed(access, chatType, groupId, senderId)) return { action: 'drop' }
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

// ─── provider 切换检测 ────────────────────────────────────────────────
// worker 启动时固化当时的 provider env；用户切换 provider 后，正在跑的 worker 仍用旧 provider。
// 这里在每条消息到达时比对【该 bot 实际生效的】指纹：变了才杀掉 worker 让它带新 provider 重生。
// 指纹只算 BASE_URL+MODEL，token 轮换不计入；不可判定（读/解析失败）时返回 null，绝不误杀。
const PROVFP_FILE = join(homedir(), '.claude', 'dispatcher', `.provfp-${BOT_NAME}`)
const PROVFP_VERSION = 'v2'
let __provFpNullStreak = 0
function currentProviderFp(): string | null {
  const { fp } = readProviderFpStatus({
    globalSharedPath: join(homedir(), '.claude', 'settings.json'),
    globalLocalPath: join(homedir(), '.claude', 'settings.local.json'),
    projectSharedPath: join(CHANNEL_DIR, '.claude', 'settings.json'),
    projectLocalPath: join(CHANNEL_DIR, '.claude', 'settings.local.json'),
  })
  if (fp) { __provFpNullStreak = 0; return fp }
  __provFpNullStreak++
  // 限频：首次 + 此后每 10 次各记一条，避免每条消息刷屏（日志不回显取值）
  if (__provFpNullStreak === 1 || __provFpNullStreak % 10 === 0) {
    process.stderr.write(`dispatcher[${BOT_NAME}]: provider 指纹不可判定已 ${__provFpNullStreak} 次（settings 读失败或解析失败）\n`)
  }
  return null
}

/** .provfp 内容带算法版本前缀：旧算法基线一律视作"无基线"，避免部署后首条消息误杀 worker。 */
function readProvFp(): string {
  try {
    const raw = readFileSync(PROVFP_FILE, 'utf8').trim()
    if (!raw.startsWith(PROVFP_VERSION + ':')) return ''
    return raw.slice(PROVFP_VERSION.length + 1)
  } catch { return '' }
}
function writeProvFp(fp: string): void {
  try {
    writeFileSync(PROVFP_FILE, `${PROVFP_VERSION}:${fp}`, { mode: 0o600 })
    chmodSync(PROVFP_FILE, 0o600)   // mode 仅对新建生效；已存在文件需显式收紧
  } catch {}
}

// ─── pause 检查 ───────────────────────────────────────────────────────
// 与 claudebotlife/pause.py 同路径：全局 ~/.claudebotlife.pause / 单 bot
// ~/.claudebotlife.pause-<bot>（跨平台：不再用 /tmp，py/ts 两侧一致）。
// pause 时连 DM 也不回。
const botPauseCheck = makePauseCheck(BOT_NAME)
function isPaused(): string | null {
  // 全局与单 bot 标记的读取规则统一在 pause_gate.ts（与 Python 侧同一组文件）
  return botPauseCheck()
}

// ─── worker spawn（经 worker-manager，headless 子进程，无 tmux）────────
function ensureWorkerRunning(_chatId: string): { uuid: string; spawned: boolean } {
  const mgr = getManager()
  const cur = currentProviderFp()
  if (mgr.isAlive()) {
    const prev = readProvFp()
    if (cur && prev && prev !== cur) {
      process.stderr.write(`dispatcher[${BOT_NAME}]: provider 变了(${prev}→${cur}) → 重启 worker\n`)
      mgr.kill()                 // 非 intentional → worker-manager 自动 --resume 重启
      writeProvFp(cur)
      return { uuid: mgr.sessionUuid, spawned: true }
    }
    if (cur && !prev) writeProvFp(cur)   // 已在跑的 worker 补记基线
    return { uuid: mgr.sessionUuid, spawned: false }
  }
  if (cur) writeProvFp(cur)              // 记录本次 spawn 用的 provider 指纹
  void mgr.ensure()
  return { uuid: mgr.sessionUuid, spawned: true }
}

// ─── provider 自动跟随（cc-switch / 手改 settings.json 后免手动重启）──────
// settings.json 的 provider 字段真变了 → 杀掉 worker，走 worker-manager 既有的崩溃重启
// 路径（backoff → --resume 续会话 → 在飞 raw 消息回插队列 → drainInbox 补投 inbox）。
// dispatcher 本体不重启，Telegram 连接不断。
startProviderWatch({
  log: line => process.stderr.write(`dispatcher[${BOT_NAME}]: provider-sync ${line}\n`),
  onProviderChanged: () => {
    const mgr = getManager()
    if (!mgr.isAlive()) {
      // 没在跑就不用动：下次 spawn 时 buildEnv 自然读到新 settings.json
      process.stderr.write(`dispatcher[${BOT_NAME}]: provider-sync worker_restart=false reason=not_running\n`)
      return
    }
    process.stderr.write(`dispatcher[${BOT_NAME}]: provider-sync worker_restart=true reason=provider_changed\n`)
    mgr.kill()   // 非 intentional → onExit 自动 --resume 重启并回插在飞消息
  },
})

// ─── attachment download (post-gate) ──────────────────────────────────
// H-14：转写与文件下载各加超时，超时只记一条日志、返回 undefined/回执，绝不无限阻塞。
const VOICE_TIMEOUT_MS = 20_000
const DOWNLOAD_TIMEOUT_MS = 20_000
const VOICE_TIMEOUT_TEXT = '[语音没转出来，稍后再发一次]'

/** 带 AbortController 的 fetch：到点即 abort（抛 AbortError 给调用方判别超时）。 */
async function fetchWithTimeout(url: string, init: RequestInit, ms: number): Promise<Response> {
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), ms)
  try { return await fetch(url, { ...init, signal: ctl.signal }) }
  finally { clearTimeout(t) }
}

/** 本机转写服务地址（可由 env 覆盖，默认 7788）。 */
function transcribeUrl(): string {
  return process.env.TRANSCRIBE_URL || 'http://127.0.0.1:7788/transcribe_telegram'
}

/** H-14：发送者是否在白名单内（私聊=access.allowFrom；群=该群策略 allowFrom，空则回退 owner 名单）。 */
function inboundSenderAllowed(ctx: Context): boolean {
  return senderAllowed(loadAccess(), ctx.chat?.type, String(ctx.chat?.id ?? ''), String(ctx.from?.id ?? ''))
}

/** H-14：dispatcher 自己用令牌取回文件字节（令牌只在服务端 URL 里用，绝不外发）。 */
async function downloadTelegramFile(fileId: string): Promise<Buffer | undefined> {
  try {
    const file = await bot.api.getFile(fileId)
    if (!file.file_path) return undefined
    const url = `${TELEGRAM_FILE_ROOT}/file/bot${TOKEN}/${file.file_path}`
    const res = await fetchWithTimeout(url, {}, VOICE_TIMEOUT_MS)
    if (!res.ok) return undefined
    return Buffer.from(await res.arrayBuffer())
  } catch (e) {
    if ((e as any)?.name === 'AbortError') throw e   // 超时交给上层记 voice_timeout
    return undefined
  }
}

async function downloadToMedia(fileId: string, chatType: string | undefined): Promise<string | undefined> {
  try {
    const file = await bot.api.getFile(fileId)
    if (!file.file_path) return undefined
    const url = `${TELEGRAM_FILE_ROOT}/file/bot${TOKEN}/${file.file_path}`
    let res: Response
    try {
      res = await fetchWithTimeout(url, {}, DOWNLOAD_TIMEOUT_MS)   // H-14：下载有上界
    } catch (e) {
      if ((e as any)?.name === 'AbortError') {
        process.stderr.write(`dispatcher[${BOT_NAME}]: download_timeout\n`)
        return undefined
      }
      throw e
    }
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

// ─── 被晾感知（有生活的追问）──────────────────────────────────────────
// 私聊回完话、她一直没回音时，按 7±2 / 25±5 / 90±15 三档决定追不追；追不追取决于
// 她此刻在做什么（作息表 interruptible）。判定全在 hang_plan.ts / hang_runtime.ts，
// 这里只接三根线：出站成功、入站、60s 定时器，外加那根查作息的 python 桥。
const HANG_TICK_MS = 60_000
// access.json 的 hangEnabled: false → 整套关掉（不武装 → 没有事件 → tick 也没得跑）
const HANG_ON = loadAccess().hangEnabled !== false

const hang = createHangRuntime({
  channelDir: CHANNEL_DIR,
  probe: () => probeSituation(BOT_NAME),
  // 内部合成消息：schema 与 scripts/self_initiate.py 那条对齐（worker-manager 的
  // readInboxMeta 按这些字段组装）。'[' 开头 → 波次层不合并它。
  // H-19：件内 synthetic:true 是合成件的唯一判据（文件名 hang- 前缀只作可读性）——
  // 这件在 bot 停用期间积压时，才能被 sweepStaleSynthetic 按陈旧清理掉。
  inject: (chatId, text) => {
    const ms = Date.now()
    writeInbox(chatId, `hang${ms}`, {
      synthetic: true,
      text,
      chat_id: chatId,
      scene: 'private',
      from_id: chatId,
      from_username: 'user',
      sender_username: 'user',
      chat_type: 'private',
      is_bot_sender: false,
      ts: new Date(ms).toISOString(),
      message_id: String(ms),
    })
  },
  log: line => process.stderr.write(`dispatcher[${BOT_NAME}]: ${line}\n`),
})
// 包一层 try：被晾感知是锦上添花的功能，出任何岔子都不能把 dispatcher 拖崩
const hangGuard = (what: string, fn: () => void): void => {
  try { fn() } catch (e) {
    process.stderr.write(
      `dispatcher[${BOT_NAME}]: hang_${what}_failed reason=${e instanceof Error ? e.name : 'unknown'}\n`)
  }
}
if (HANG_ON) {
  // 被晾件在 bot 停用期间会积压；它带 synthetic 标记但文件名不带 hang- 前缀，readInboxMeta 的
  // 前缀判定兜不住它——按件内标记 + 件内 ts 做陈旧清理（H-19），与 hang.tick 同一把节奏。
  const sweepInbox = () => {
    sweepStaleSynthetic({
      dir: join(CHANNEL_DIR, 'inbox'), nowMs: Date.now(), ttlMs: STALE_SYNTHETIC_MS,
      log: line => process.stderr.write(`dispatcher[${BOT_NAME}]: ${line}\n`),
    })
  }
  // 被晾追问归「生活作息」，关着时 tick 不跑（出站那头同样不武装）
  setInterval(() => {
    if (featureOn(botFeatures(), 'life')) hangGuard('tick', hang.tick)
    hangGuard('sweep', sweepInbox)
  }, HANG_TICK_MS)
}

// ─── group transcript (post-gate) ─────────────────────────────────────
function writeGroupTranscript(ctx: Context, text: string, imagePath?: string, attachment?: { kind: string; file_id: string }): void {
  const chatType = ctx.chat?.type
  if (chatType !== 'group' && chatType !== 'supergroup') return
  // 这只 bot 的群聊关着 → 它的调度器不写任何群记录
  if (!featureOn(botFeatures(), 'groups')) return
  try {
    // 跨 bot 去重：同群的 N 个 dispatcher 进程都会收到同一条消息，若各写一行，director
    // 会看到同一句话 ×N → 误判热度爆表 → 连续指派多个 bot 全部回复（实测 bug）。
    // 用 O_EXCL 标志文件原子抢占：谁抢到谁写，其余进程静默跳过。放系统临时目录重启自清。
    if (ctx.message?.message_id != null) {
      const dedupDir = join(tmpdir(), 'tg-transcript-dedup')
      mkdirSync(dedupDir, { recursive: true })
      try {
        writeFileSync(join(dedupDir, `${ctx.chat!.id}_${ctx.message.message_id}`), botUsername, { flag: 'wx' })
      } catch { return }  // 已有别的 bot 写过这条 → 跳过
      // 低频顺手清理 24h+ 的旧标志，防小文件无限堆积
      if (Math.random() < 0.02) {
        try {
          const cutoff = Date.now() - 24 * 3600_000
          for (const f of readdirSync(dedupDir)) {
            const p = join(dedupDir, f)
            try { if (statSync(p).mtimeMs < cutoff) rmSync(p, { force: true }) } catch {}
          }
        } catch {}
      }
    }
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

// ─── clear 前先把"最近聊了什么"写进自动记忆 ────────────────────────────
// clear 掉会话，正在聊的话题就没了（MEMORY.md 只存长期稀疏事实）。这里在两条清空
// 路径（/clear 走 stdin、/clearall 走 kill+删会话）真正执行之前，先把最近对话压成
// 摘要写进 ~/.claude/projects/<slug>/（memory/ 子目录存正文，MEMORY.md 存索引）。
// 判断与落地逻辑全在 clear_summary.ts。铁律：摘要任何一环失败都只降级、绝不阻断 clear 本身。
const SESSION_JSONL = (() => {
  try {
    return join(homedir(), '.claude', 'projects', projectSlug(CHANNEL_DIR), `${sessionUuid()}.jsonl`)
  } catch { return '' }   // 没配命名空间 → 整个摘要功能降级为 no-op
})()
const PROJECT_DIR = SESSION_JSONL ? dirname(SESSION_JSONL) : ''
const SUMMARY_TAIL_BYTES = 2 * 1024 * 1024   // 会话 jsonl 可达几百 MB，只读尾巴
const SUMMARY_TIMEOUT_MS = 25_000
// /clear 回执上限：回执只是通知，挂住了也不能让 clearInProgress 陪着等 grammY 默认 500s 的请求超时
const RECEIPT_TIMEOUT_MS = 10_000
// H-12：等旧 worker exit 事件的上限；超时强制放行门禁并照常移会话文件（记 wm_clear_exit_timeout）
const CLEAR_EXIT_TIMEOUT_MS = 10_000
let clearInProgress = false
// 清理进行中压下没拉的 worker（消息照常进 inbox）：杀 worker 到移走会话文件之间拉起，
// 会 --resume 那份旧会话，清了等于没清。
let clearDeferredSpawn = ''

/** 清理收尾：放开 clearInProgress；期间被压着没拉 worker 的，这里补拉一次。 */
function releaseClear(): void {
  clearInProgress = false
  const chat = clearDeferredSpawn
  clearDeferredSpawn = ''
  if (chat) ensureWorkerRunning(chat)
}

function readTailFile(path: string, maxBytes: number): string {
  let fd = -1
  try {
    const size = statSync(path).size
    const len = Math.min(size, maxBytes)
    if (len <= 0) return ''
    fd = openSync(path, 'r')
    const buf = Buffer.allocUnsafe(len)
    readSync(fd, buf, 0, len, size - len)
    return buf.toString('utf8')
  } catch { return '' }
  finally { if (fd >= 0) try { closeSync(fd) } catch {} }
}

// 摘要子进程的 env 白名单：不注入任何凭证，让 claude 自己读 settings.json / 钥匙串。
// USER 必须给（缺了 macOS 上读不到钥匙串凭证，直接 "Not logged in" 快败）；Windows 侧
// 的 USERPROFILE/APPDATA/SYSTEMROOT 同理，缺一个就起不来。大小写不敏感匹配，避免
// Windows 上造出 PATH/Path 双键。
const SUMMARY_ENV_KEYS = new Set([
  'PATH', 'PATHEXT', 'COMSPEC', 'HOME', 'TMPDIR', 'TEMP', 'TMP',
  'USER', 'LOGNAME', 'USERNAME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
  'APPDATA', 'LOCALAPPDATA', 'SYSTEMROOT', 'WINDIR',
  'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
])

/**
 * claude -p 摘要子进程。异步（同步调用会把整个 bot 的事件循环卡住 25 秒）。
 * cwd 用临时目录：不能是 bot 目录（会在 bot 的 projects 里多出 jsonl，还会吃 .mcp.json）。
 * --strict-mcp-config 且不带 --mcp-config → 不加载任何 MCP server。
 * 超时/非 0 退出/空输出 → null；认证失败 → AUTH_FAILED 哨兵，由 summarizeTurns 重试一次。
 */
function runClaudeSummary(prompt: string): Promise<string | null> {
  return new Promise(resolve => {
    let settled = false
    const finish = (v: string | null) => { if (!settled) { settled = true; resolve(v) } }
    let cwd = ''
    try {
      cwd = mkdtempSync(join(tmpdir(), 'clear-summary-'))
      const env: Record<string, string> = {}
      for (const [k, v] of Object.entries(process.env)) {
        if (typeof v === 'string' && SUMMARY_ENV_KEYS.has(k.toUpperCase())) env[k] = v
      }
      if (!env.PATH && !env.Path) env.PATH = '/usr/bin:/bin:/usr/local/bin'
      if (!env.HOME && !env.USERPROFILE) env.HOME = homedir()
      const cleanup = () => { try { if (cwd) rmSync(cwd, { recursive: true, force: true }) } catch {} }
      const claude = resolveClaude()
      const args = ['-p', '--strict-mcp-config']
      // Windows .cmd 得经 cmd /d /s /c 起，引号拼法与 worker-manager 共用 win_cmd.ts（两份内联复制曾修一处漏一处）。
      // spec 只在 viaCmd 分支算：winCmdLine 的 %…%/!…! 守卫会抛错，POSIX 路径不该受 cmd 规则约束（抛错由外层 catch 接住 → 摘要为空）
      const spawnViaCmd = () => {
        const spec = winCmdSpawnSpec(claude.bin, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] })
        return spawn(spec.file, spec.args, spec.opts as any)
      }
      const p = claude.viaCmd ? spawnViaCmd() : spawn(claude.bin, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] })
      let out = ''
      let err = ''
      // Windows 上 p 是 cmd 壳，p.kill 只杀壳、claude(node.exe) 成孤儿且占着临时 cwd 删不掉 → 按进程树杀（POSIX 仍是 SIGKILL）
      const timer = setTimeout(() => { killTree(p.pid ?? 0); cleanup(); finish(null) }, SUMMARY_TIMEOUT_MS)
      p.stdout.on('data', d => { if (out.length < 64_000) out += String(d) })
      p.stderr.on('data', d => { if (err.length < 4_000) err += String(d) })   // 只用于认错识别，不落日志
      p.on('error', () => { clearTimeout(timer); cleanup(); finish(null) })
      p.on('close', code => {
        clearTimeout(timer); cleanup()
        if (code === 0 && out.trim()) return finish(out)
        finish(/not logged in/i.test(err) ? AUTH_FAILED : null)
      })
      p.stdin.on('error', () => {})
      p.stdin.end(prompt)
    } catch {
      try { if (cwd) rmSync(cwd, { recursive: true, force: true }) } catch {}
      finish(null)
    }
  })
}

/**
 * 摘要 → 写记忆（H-24）。先按元数据判"无轮次 / 有轮次但提取为空"，这两种都不写、保留旧摘要；
 * 有轮次且提取到 reply 原文才走模型摘要并原子覆盖。返回三种情形供回执文案用。全程 try 包住，绝不抛。
 */
async function saveClearSummary(scope: 'all' | 'dm'): Promise<ClearSummaryResult> {
  const dlog = (l: string) => process.stderr.write(`dispatcher[${BOT_NAME}]: ${l}\n`)
  try {
    if (!SESSION_JSONL) return { status: 'empty_skip', turns: 0, receipt: RECEIPT_CLEARED_SKIP }
    const tail = readTailFile(SESSION_JSONL, SUMMARY_TAIL_BYTES)
    const { turns, replyTurns } = parseRecentTurns(tail, { scope })
    if (turns.length === 0) {
      dlog('clear_summary_empty_skip')
      return { status: 'empty_skip', turns: 0, receipt: RECEIPT_CLEARED_SKIP }
    }
    if (replyTurns === 0) {
      dlog(`clear_summary_extract_empty turns=${turns.length}`)
      return { status: 'extract_empty', turns: turns.length, receipt: RECEIPT_NO_SUMMARY }
    }
    // 只有确实有可提取的对话才花一次模型子进程；空轮次不再白跑
    const body = await summarizeTurns(turns, runClaudeSummary)
    process.stderr.write(`dispatcher[${BOT_NAME}]: clear_summary scope=${scope} turns=${turns.length} body=${body.length > 0}\n`)
    return runClearSummary({ sessionText: tail, memoryDir: PROJECT_DIR, scope, body, log: dlog })
  } catch {
    process.stderr.write(`dispatcher[${BOT_NAME}]: clear_summary failed (clear 照常继续)\n`)
    return { status: 'extract_empty', turns: 0, receipt: RECEIPT_NO_SUMMARY }
  }
}

/** /clearall 的"真清"：备份成 .deleted-<ts> 再移走，下次 spawn 自动起全新会话 */
function wipeSessionJsonl(): boolean {
  try {
    if (!SESSION_JSONL || !existsSync(SESSION_JSONL)) return false
    const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+$/, '')
    renameSync(SESSION_JSONL, `${SESSION_JSONL}.deleted-${ts}`)
    return true
  } catch {
    process.stderr.write(`dispatcher[${BOT_NAME}]: wipe session failed\n`)
    return false
  }
}

// ─── 承诺兑现（说过的话到点必须兑现）────────────────────────────────────
// bot 说"20 分钟后找你"→ 模型调 promise_set → 落 .promises.json（本进程是唯一写者）→
// 60s tick 到点注入 [promise-due]。判睡且无近期入站 → 转欠账，由下一次投递捎上认账。
// 决策在 promise_plan.ts，编排在 promise_runtime.ts，这里只接三根线。
const PROMISE_TICK_MS = 60_000
const PROMISE_TICK_OFFSET_MS = 30_000    // 与 hang tick 错开半分钟，两个 probe 不撞在同一秒

/** 到期时 worker 不在 → 异步拉活（worker-manager 是唯一权威，不引入 tmux/launchctl）。 */
function promiseEnsureWorker(chatId: string): void {
  if (clearInProgress) { clearDeferredSpawn = chatId; return }
  const mgr = getManager()
  if (mgr.isAlive()) return                       // worker 在线，inbox 里的件它自己会 drain
  process.stderr.write(`dispatcher[${BOT_NAME}]: promise_spawn_worker chat=${chatId}\n`)
  void mgr.ensure().catch(e => {
    process.stderr.write(`dispatcher[${BOT_NAME}]: promise_spawn_failed reason=${e instanceof Error ? e.name : 'unknown'}\n`)
  })
}

const promiseBotDisabled = makeBotDisabledCheck(BOT_NAME)
const promiseLog = (line: string) => { process.stderr.write(`dispatcher[${BOT_NAME}]: ${line}\n`) }
const promise = createPromiseRuntime({
  channelDir: CHANNEL_DIR,
  inject: (chatId, key, meta) => writeInbox(chatId, key, meta),
  retract: makeInboxRetract(CHANNEL_DIR),
  ...makeSessionScanner(CHANNEL_DIR, BOT_NAME),
  probeAsleep: makeAsleepProbe(BOT_NAME, undefined, undefined, promiseLog),
  ensureWorker: promiseEnsureWorker,
  // pause 时连 DM 都不回，承诺更不该发 → 顺延；bot 停用开关同一道闸
  isPaused: () => isPaused() ?? promiseBotDisabled(),
  isStopped: makeStopCheck(BOT_NAME),
  log: promiseLog,
})
// 承诺功能的任何失败都不得把 dispatcher 拖崩或停掉真人消息处理。
const promiseTick = () => {
  promise.tick().catch(e =>
    process.stderr.write(`dispatcher[${BOT_NAME}]: promise_tick_failed reason=${e instanceof Error ? e.name : 'unknown'}\n`))
}
promiseTick()                                      // 启动即补一轮：重启期间错过的到期立刻补上
setTimeout(() => setInterval(promiseTick, PROMISE_TICK_MS), PROMISE_TICK_OFFSET_MS)

// ─── 清空类命令鉴权（H-13）──────────────────────────────────────────────
// bot 用户名：@ 后缀必须与它一致，否则按未授权处理（不得"忽略后缀继续执行"）
function botUsernameOf(ctx: Context): string {
  const m = (ctx as any).me?.username
  return String(m || botUsername || process.env.TELEGRAM_BOT_USERNAME || BOT_NAME || '').replace(/^@/, '')
}

/** 归一化清空类命令判定：/clear、/clearall、/clear_all、大小写不敏感、剥前导 @mention。
 *  返回 { suffix }（空串=未带 @后缀）或 null（非清空类命令）。 */
function parseClearCommand(text: string): { suffix: string } | null {
  const t = text.trim().replace(/^@\w+\s+/, '').trim()
  const m = /^\/([a-z0-9_]+)(?:@([a-z0-9_]+))?$/i.exec(t)
  if (!m) return null
  const cmd = m[1].toLowerCase().replace(/_/g, '')
  if (cmd !== 'clear' && cmd !== 'clearall') return null
  return { suffix: (m[2] ?? '').toLowerCase() }
}

/** 拒绝路径回一条固定文案（恰好一次）；回执失败不影响判定。 */
function replyClearDenied(ctx: Context): void {
  const msg = '[清空是主人私聊专属操作，这里不执行]'
  const c = ctx as any
  try {
    const send = typeof c.reply === 'function' ? c.reply
      : typeof c.replyWithMarkdown === 'function' ? c.replyWithMarkdown
      : typeof c.replyWithHTML === 'function' ? c.replyWithHTML
      : null
    const r = send ? send.call(c, msg)
      : (c.api && typeof c.api.sendMessage === 'function' ? c.api.sendMessage(String(ctx.chat?.id ?? ''), msg) : undefined)
    if (r && typeof r.catch === 'function') r.catch(() => {})
  } catch { /* 回执失败不影响判定 */ }
}

/**
 * 清空类命令鉴权（H-13）——只判定，不执行清空。
 * 放行条件：私聊 + sender 在私聊名单（access.allowFrom；群名单里的 id 一律不算主人）；
 * 且若带 @后缀，后缀必须等于本 bot 用户名。
 * 拒绝路径零副作用（不 kill、不移会话文件），回一条固定文案后返回 false。
 * 非清空类命令返回 false 且不回执（调用方本就不该拿它判清空）。
 */
export function authorizeClear(ctx: Context, textArg?: string): boolean {
  const text = textArg ?? ctx.message?.text ?? (ctx.msg as any)?.text ?? ''
  const cmd = parseClearCommand(text)
  if (!cmd) return false
  const isPrivate = ctx.chat?.type === 'private'
  const isOwner = isPrivate && loadAccess().allowFrom.includes(String(ctx.from?.id ?? ''))
  const suffixOk = cmd.suffix === '' || cmd.suffix === botUsernameOf(ctx).toLowerCase()
  if (isOwner && suffixOk) return true
  replyClearDenied(ctx)
  return false
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
  const replyOpt = msgId != null ? { reply_parameters: { message_id: msgId } } : {}

  // ─── /stopbot | /startbot：dispatcher 层直接执行，不下发 AI ───
  // 双保险鉴权：仅私聊 + allowFrom 白名单；拒绝时静默吞掉，不给探测者回执。
  const ctl = parseBotCtl(trimmed)
  if (ctl) {
    const sender = String(ctx.from?.id ?? '')
    const acc0 = loadAccess()
    if (ctx.chat?.type !== 'private' || !acc0.allowFrom.includes(sender)) {
      process.stderr.write(`dispatcher[${BOT_NAME}]: botctl 拒绝 chat=${chatId} from=${sender}\n`)
      return true
    }
    if ('usage' in ctl) {
      await bot.api.sendMessage(chatId, ctl.usage, replyOpt).catch(() => {})
      return true
    }
    // 公开版没有 stop-bot.sh / start-bot.sh：不 spawn 任何脚本，只给管理台操作提示。
    await bot.api.sendMessage(chatId, botCtlNotice(ctl), replyOpt).catch(() => {})
    process.stderr.write(`dispatcher[${BOT_NAME}]: botctl ${ctl.cmd} ${ctl.target} by ${sender}（公开版仅提示 8767 管理台）\n`)
    return true
  }

  // /clear、/clearall 等清空类命令已在 handleInbound 步骤 1a 统一鉴权处理
  // （H-13：仅"私聊 + 主人 + 后缀为本 bot"放行，群/非主人一律拒绝），此处不再重复。

  try {
    getManager().sendSlash(trimmed)  // 排队走 stdin，manager 自己等 ready，无竞态
    await bot.api.sendMessage(chatId, `已下发: ${trimmed}`, replyOpt)
  } catch (e) {
    await bot.api.sendMessage(chatId, `[slash bridge 失败: ${String(e).slice(0, 200)}]`, replyOpt).catch(() => {})
  }
  return true
}

// ─── inbound handler ──────────────────────────────────────────────────
type AttachmentMeta = { kind: string; file_id: string; size?: number; mime?: string; name?: string }

// ─── party-camera：入站判定（gate 之后、写 inbox 之前）──────────────────
// 只按 meta 给 worker 打标；日志只记聊天号/轮次/原因码，绝不带用户正文。
function logPhotoStateProblem(chatId: string, status: 'unreadable' | 'clock_skew' | 'expired'): void {
  const reason = status === 'clock_skew' ? 'state_clock_skew'
    : status === 'expired' ? 'photo_state_expired' : 'photo_state_unreadable'
  process.stderr.write(`dispatcher[${BOT_NAME}]: ${reason} kind=photo chat=${chatId}\n`)
}

/** 聚会状态不可用：不可读/时钟回拨/过期各记固定原因码，只带聊天号。 */
function logPartyStateProblem(chatId: string, status: string): void {
  const reason = status === 'clock_skew' ? 'state_clock_skew'
    : status === 'expired' ? 'party_state_expired'
    : status === 'unreadable' ? 'party_state_unreadable' : ''
  if (reason !== '') process.stderr.write(`dispatcher[${BOT_NAME}]: ${reason} kind=party chat=${chatId}\n`)
}

/** 拍照：image 关 → 删状态；退出 → photo_mode_off；触发 → 建/重置状态；否则读状态给 active。 */
function inboundPhotoMeta(chatId: string, messageId: string, text: string, nowMs: number): Record<string, unknown> | null {
  if (!isValidPhotoChatId(chatId)) return null
  const path = photoStatePath(PHOTO_MODE_DIR, BOT_NAME, chatId)
  if (!featureOn(botFeatures(), 'image')) {
    if (existsSync(path)) {
      deletePhotoState(path)
      process.stderr.write(`dispatcher[${BOT_NAME}]: photo_feature_off chat=${chatId}\n`)
    }
    return null
  }
  if (isPhotoExit(text)) {
    deletePhotoState(path)
    return { photo_mode_off: true }
  }
  if (parsePhotoTrigger(text)) {
    const current = readPhotoState(path, nowMs)
    // 同一 trigger_message_id 重复到达：不重置轮次与开启时刻
    if (current.status === 'ok' && current.state && messageId !== '' && current.state.trigger_message_id === messageId) {
      return { photo_mode: photoMeta(current.state) }
    }
    const state = triggerPhotoState(chatId, messageId, nowMs)
    if (!writePhotoState(path, state)) {
      process.stderr.write(`dispatcher[${BOT_NAME}]: photo_state_write_failed chat=${chatId}\n`)
      return null
    }
    return { photo_mode: photoMeta(state) }
  }
  const current = readPhotoState(path, nowMs)
  if (current.status === 'ok' && current.state) return { photo_mode: photoMeta(current.state) }
  if (current.status !== 'missing') {
    logPhotoStateProblem(chatId, current.status)
    deletePhotoState(path)
  }
  return null
}

/** 聚会改投：只对 owner 私聊；逃生口前缀只打 party_opt_out；多个有效群 → party_ambiguous。 */
function inboundPartyMeta(chatId: string, text: string, nowMs: number, access: Access): Record<string, unknown> | null {
  const owner = ownerOf(access)
  if (owner === '' || chatId !== owner) return null
  if (!featureOn(botFeatures(), 'groups')) return null
  if (isPartyOptOutPrefix(text)) return { party_opt_out: true }
  const found = participatingPartyGroups({
    partyRoot: PARTY_MODE_DIR, accessGroups: access.groups, botName: BOT_NAME,
    owner, groupsFeatureOn: true, nowMs,
  })
  for (const bad of found.invalid) {
    logPartyStateProblem(bad.chat_id, bad.status)
    deletePartyState(partyStatePath(PARTY_MODE_DIR, bad.chat_id))
  }
  if (found.invalidNames.length > 0) {
    process.stderr.write(`dispatcher[${BOT_NAME}]: party_state_invalid_name kind=party count=${found.invalidNames.length}\n`)
  }
  if (isPartyEndPhrase(text)) {
    if (found.groups.length === 1) {
      const g = found.groups[0]!
      if (deletePartyState(partyStatePath(PARTY_MODE_DIR, g.chat_id))) {
        process.stderr.write(`dispatcher[${BOT_NAME}]: party_off chat=${g.chat_id} reason=phrase_private\n`)
      }
    }
    return null
  }
  if (found.groups.length === 0) return null
  if (found.groups.length > 1) return { party_ambiguous: true }
  const g = found.groups[0]!
  process.stderr.write(`dispatcher[${BOT_NAME}]: cross_chat_party src=${chatId} dst=${g.chat_id}\n`)
  return { party_chat_id: g.chat_id, party_source: g.state.source }
}

async function handleInbound(
  ctx: Context,
  text: string,
  imageSource?: (() => Promise<string | undefined>) | string,
  attachment?: AttachmentMeta,
  voiceText?: string,
  options?: { synthetic?: boolean },
): Promise<void> {
  // party-camera：触发/退出/结束短语一律用入站原文匹配；系统附加提示不进匹配。
  const originalText = text
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
    // 发现记录：陌生群建一条（added_by_owner=null），已有记录只更新 title/last_seen。
    // 不看 groups 开关，也不管后面这条消息会不会被路由。
    recordSeenGroupMessage({ id: ctx.chat!.id, title: ctx.chat!.title, type: ctx.chat!.type }, !prePolicy)
    if (!prePolicy) return
    const preFromUsername = ctx.from?.username ? `@${ctx.from.username}` : ''
    const preIsOtherBot = ctx.from?.is_bot === true && preFromUsername.length > 0 &&
      (prePolicy.otherBotUsernames ?? []).some(u => u.toLowerCase() === preFromUsername.toLowerCase())
    // 与 gate 同一套判定：空 allowFrom 不再等于“所有人放行”，peer bot 仍豁免。
    if (!preIsOtherBot &&
        !senderAllowed(preAccess, chatType, groupChatId, String(ctx.from?.id ?? ''))) return
    // party-camera：过 allowlist 预筛后记录本群最后一条真人消息发送者与 owner 最近发言时刻。
    if (!isBotSender && !options?.synthetic) {
      const senderId = String(ctx.from?.id ?? '')
      lastHumanSenderId.set(groupChatId, senderId)
      const preOwner = ownerOf(preAccess)
      if (preOwner !== '' && senderId === preOwner) lastOwnerGroupMsgAtMs.set(groupChatId, nowMsFromEnv())
    }
  }

  // 群聊关着：群消息过了白名单预筛就到此为止——不加 👀、不执行群命令、不写收件与群记录、
  // 不拉起。放在 /clearall 之前，避免关着也走完整个清理流程。
  if (isGroup && !featureOn(botFeatures(), 'groups')) {
    process.stderr.write(`dispatcher[${BOT_NAME}]: group_feature_off 群聊功能关着，群消息不往下处理 chat=${groupChatId}\n`)
    return
  }

  // 1a. 清空类命令（/clear、/clearall、别名、@后缀）统一鉴权（H-13）。
  //     仅"私聊 + 主人（私聊名单 allowFrom 的 id，群名单不算）+ @后缀为本 bot"放行；
  //     群里任何形式、私聊非主人、后缀不符一律拒绝：不 kill、不移会话文件，回一条固定文案后返回。
  //     跳过 synthetic peer-injected msgs 与 bot senders（避免循环）。
  //
  // unified：只有一个 worker（群+私聊同脑）。只 kill worker 清不掉记忆——下次 spawn 又 --resume
  // 同一会话文件，故真清（备份后移走会话），并在删除前把最近对话压进自动记忆（saveClearSummary）。
  if (!isBotSender && !options?.synthetic && parseClearCommand(text)) {
    if (!authorizeClear(ctx, text)) return   // 拒绝路径已在 authorizeClear 内回执
    const chatId = String(ctx.chat!.id)
    const msgId = ctx.message?.message_id
    const replyOpt = msgId != null ? { reply_parameters: { message_id: msgId } } : {}
    if (clearInProgress) {
      await bot.api.sendMessage(chatId, '[上一次清理还没走完，稍等]', replyOpt).catch(() => {})
      return
    }
    clearInProgress = true
    // 回执与清理解耦：走到回执时上下文已清，回执失败/挂起只记日志，不再回"失败"误导用户。
    const receipt = async (msg: string): Promise<void> => {
      let timer: ReturnType<typeof setTimeout> | undefined
      try {
        await Promise.race([
          bot.api.sendMessage(chatId, msg, replyOpt),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), RECEIPT_TIMEOUT_MS) }),
        ])
      } catch (e) {
        process.stderr.write(`dispatcher[${BOT_NAME}]: clear_receipt_failed chat=${chatId} ${(e instanceof Error ? e.message : String(e)).slice(0, 200)}\n`)
      } finally {
        clearTimeout(timer)
      }
    }
    void (async () => {
      const mgr = getManager()
      mgr.beginClear()   // H-12：单一门禁——先置位（ensure 内先查），再走后续，判定到 spawn 无 await 空档
      try {
        const summary = await saveClearSummary('all')       // 摘要必须先于删除
        // H-12：杀 worker 并等旧进程 exit 事件到达（上限 CLEAR_EXIT_TIMEOUT_MS；超时强制放行）
        const r = await mgr.killAndWaitExit(CLEAR_EXIT_TIMEOUT_MS)
        const wiped = wipeSessionJsonl()                   // 移会话文件必须在 exit 之后
        process.stderr.write(`dispatcher[${BOT_NAME}]: wm_clear_moved exit=${r} wiped=${wiped} summary=${summary.status}\n`)
        await receipt(summary.receipt)
      } catch (e) {
        process.stderr.write(`dispatcher[${BOT_NAME}]: clear failed chat=${chatId} ${String(e).slice(0, 200)}\n`)
        await receipt(`[清空没成功: ${String(e).slice(0, 200)}]`)
      } finally {
        mgr.endClear()                     // H-12：含异常路径必须释放门禁，绝不永久持门
        releaseClear()                     // 清理期间压下的 inbound 在这里补拉 worker
      }
    })()
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

  // party-camera：群内 owner 的整句结束短语在 gate 前处理——导演模式下群消息会被 gate 全 drop，
  // 也仍要能结束聚会。只认 owner；删除幂等。
  if (isGroup && groupChatId && isValidGroupId(groupChatId) && !isBotSender && !options?.synthetic && isPartyEndPhrase(originalText)) {
    const endOwner = ownerOf(loadAccess())
    if (endOwner !== '' && String(ctx.from?.id ?? '') === endOwner) {
      const path = partyStatePath(PARTY_MODE_DIR, groupChatId)
      const had = existsSync(path)
      if (!deletePartyState(path)) {
        process.stderr.write(`dispatcher[${BOT_NAME}]: party_off_failed chat=${groupChatId} reason=phrase\n`)
      } else if (had) {
        process.stderr.write(`dispatcher[${BOT_NAME}]: party_off chat=${groupChatId} reason=phrase\n`)
      }
    }
  }

  // 3. Gate
  const result = gate(ctx)
  if (result.action === 'drop') { process.stderr.write(`dispatcher[${BOT_NAME}]: gate DROP chat=${ctx.chat?.id} from=${ctx.from?.id}\n`); return }
  const access = result.access

  // party-camera 安全：记录已路由的真人入站件（chat|message_id → 发送者），供 /party 绑定校验。
  if (isGroup && groupChatId && !isBotSender && !options?.synthetic && ctx.message?.message_id != null) {
    rememberInboundHumanSender(groupChatId, String(ctx.message.message_id), String(ctx.from!.id), nowMsFromEnv())
  }

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

  // ensure worker。/clear、/clearall 进行中不拉起（见 clearDeferredSpawn），消息照常写 inbox。
  if (clearInProgress) clearDeferredSpawn = chatId
  else ensureWorkerRunning(chatId)

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
  // 真人（非 bot、非合成）消息打 human_ts / mentions_group 标，worker 据此统一计时、找"最近真人在哪个聊天"（INTERFACE §4.1）
  Object.assign(meta, inboxHumanMarks(text, isBotSender, options?.synthetic, ctx.message?.date, Date.now()))
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
  // party-camera：只在「已过 gate 的真人、非合成、非斜杠」上判定；入站只决定是否处于拍照
  // 模式（不增加轮次）；轮次由投递时的 /photo_round 增加。
  if (!meta.is_bot_sender && !options?.synthetic && !originalText.trim().startsWith('/')) {
    const nowMs = nowMsFromEnv()
    Object.assign(meta, inboundPhotoMeta(chatId, msgId != null ? String(msgId) : '', originalText, nowMs) ?? {})
    if (!isGroup) Object.assign(meta, inboundPartyMeta(chatId, originalText, nowMs, access) ?? {})
  }
  const inboxKey = msgId != null ? String(msgId) : `t${Date.now()}-${++__inboxFallbackSeq}`
  writeInbox(chatId, inboxKey, meta)
  process.stderr.write(`dispatcher[${BOT_NAME}]: routed chat=${chatId} inboxKey=${inboxKey} synthetic=${!!options?.synthetic}\n`)

  // ─── 写 last-user marker（给 self-initiate / chat_history 算 since_last_user_min 用） ───
  // 仅当真人私聊（非 bot sender、非 synthetic peer 注入）时才更新。否则 since 会
  // 被 peer-inbound / synthetic 重置为 0，self-initiate 永远进入"非静默期"被拦下。
  if (!meta.is_bot_sender && !options?.synthetic && (meta.chat_type === 'private' || ctx.chat?.type === 'private')) {
    // 她回话了 → 取消这条 chat 上的被晾事件（也记下最后入站时刻，供出站时判"这是回她的话"）
    hangGuard('cancel', () => hang.onInbound(chatId))
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
bot.on('my_chat_member', ctx => recordMyChatMember(ctx))
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
  const chatId = String(ctx.chat!.id)
  // 群聊关着：群里来的语音不下载、不转写，原样交给 handleInbound 走群开关那一步。
  const voiceChatType = ctx.chat?.type
  if ((voiceChatType === 'group' || voiceChatType === 'supergroup') && !featureOn(botFeatures(), 'groups')) {
    return handleInbound(ctx, ctx.message.caption ?? '(voice message)', undefined,
      { kind: 'voice', file_id: voice.file_id, size: voice.file_size, mime: voice.mime_type })
  }
  // H-14：先过白名单再转写 —— 非白名单直接跳过，绝不调转写服务、绝不上送令牌。
  if (!inboundSenderAllowed(ctx)) {
    process.stderr.write(`dispatcher[${BOT_NAME}]: voice_skipped reason=not_allowed chat=${chatId}\n`)
    return
  }
  let text = ctx.message.caption ?? '(voice message)'
  let voiceText: string | undefined
  try {
    const bytes = await downloadTelegramFile(voice.file_id)
    if (bytes) {
      // H-14 §9b：只把音频字节 POST 给本机转写服务，请求体绝不含 bot token。
      const r = await fetchWithTimeout(transcribeUrl(),
        { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', ...voiceBridgeAuthHeaders() }, body: bytes },
        VOICE_TIMEOUT_MS)
      if (r.ok) {
        const j = await r.json() as { text?: string }
        if (j.text && j.text.trim()) { text = j.text.trim(); voiceText = text }
      }
    }
  } catch (e) {
    if ((e as any)?.name === 'AbortError') {
      // H-14 §8/18：转写超时 → 记 voice_timeout 并给该聊天回一条固定文案（不含正文）。
      process.stderr.write(`dispatcher[${BOT_NAME}]: voice_timeout chat=${chatId}\n`)
      await bot.api.sendMessage(chatId, VOICE_TIMEOUT_TEXT).catch(() => {})
      return
    }
  }
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
/** 单段发送的最长等待：超过它就不再同步死等（调用方 MCP 60s 超时，等下去必然被判超时 → AI 重发） */
const MAX_RETRY_WAIT_MS = 25_000
// /send 单段发送的最大尝试次数（循环上界与日志文案共用此值，勿再写死 3）
const MAX_SEND_ATTEMPTS = 3
/** 被 Telegram 限流且等不起：/send 据此回 429 + retry_after，让 AI 知道"发不出去"而不是"没发" */
class RateLimited extends Error {
  constructor(public retryAfterSec: number) { super(`rate_limited retry_after=${retryAfterSec}s`) }
}

// ─── H-16/H-17：/send 幂等台账与耗时上界 ─────────────────────────────
/** 整条 /send 请求的总耗时上界：到界停止后续段、未发段记 timeout（r1 审查 16）。 */
const SEND_TOTAL_BUDGET_MS = 60_000
/** 幂等台账：TTL 10 分钟、上限 100 条（LRU 挤出最旧）。 */
const BATCH_TTL_MS = 10 * 60 * 1000
const BATCH_MAX = 100
interface BatchEntry { at: number; status: number; text: string }
interface Ledger { map: Map<string, BatchEntry> }
function ledgerLookup(led: Ledger, id: string, now: number): BatchEntry | undefined {
  const e = led.map.get(id)
  if (!e) return undefined
  if (now - e.at > BATCH_TTL_MS) { led.map.delete(id); return undefined }
  return e
}
function ledgerStore(led: Ledger, id: string, status: number, text: string, now: number): void {
  led.map.delete(id)                     // 覆盖时视为最新（LRU）
  led.map.set(id, { at: now, status, text })
  while (led.map.size > BATCH_MAX) {     // 上限：挤出最早一条
    const k = led.map.keys().next().value
    if (k == null) break
    led.map.delete(k)
  }
}

/** 段失败原因归类（契约 11 的固定集合）。绝不回显错误正文（防用户正文/令牌外泄）。 */
function segmentReason(e: unknown): string {
  if (e instanceof GrammyError) {
    const c = (e as any).error_code
    if (typeof c === 'number' && c >= 400 && c < 500) return 'telegram_4xx'
    if (typeof c === 'number' && c >= 500) return 'telegram_5xx'
    return 'network'
  }
  const name = (e as any)?.name
  if (name === 'AbortError' || name === 'TimeoutError') return 'timeout'
  // sendFileBuffered 抛的是 `new Error(\`${method} ${status}: ...\`)` → 从中取状态码归类。
  const msg = String((e as any)?.message ?? e ?? '')
  const m = /^[A-Za-z]+ (\d{3})\b/.exec(msg)
  if (m) {
    const c = Number(m[1])
    if (c >= 400 && c < 500) return 'telegram_4xx'
    if (c >= 500) return 'telegram_5xx'
  }
  return 'network'
}

/** 是否为"引用目标不存在"类错误（据此去掉引用参数重发一次）。 */
function isReplyRefError(e: unknown): boolean {
  if (!(e instanceof GrammyError)) return false
  if ((e as any)?.error_code !== 400) return false
  const d = String((e as any)?.description ?? (e as any)?.message ?? '')
  return /to be replied not found|reply message not found|replied message not found/i.test(d)
}

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

// 手工缓冲 multipart 发文件到 Telegram。
// 为什么不用 grammy 的 bot.api.sendPhoto：它把文件包成流式 body（chunked，无 Content-Length），
// 而 bun 的 fetch 经 HTTPS_PROXY 发流式 body 会失败（"Network request failed"）。JSON 的
// sendMessage 能通、curl 传图能通，都因为带 Content-Length。这里把整个 multipart 拼成单个
// Buffer（bun 自动带 Content-Length，非流式），走同一条 bun fetch 代理 → 稳定，像 curl 一样。
async function sendFileBuffered(
  method: 'sendPhoto' | 'sendDocument', field: 'photo' | 'document',
  chatId: string, filePath: string, replyToId?: number,
): Promise<number> {
  const data = readFileSync(filePath)
  const boundary = `----tgbot${Date.now().toString(16)}${data.length.toString(16)}`
  const CRLF = '\r\n'
  const parts: Buffer[] = []
  const textField = (name: string, value: string) =>
    parts.push(Buffer.from(`--${boundary}${CRLF}Content-Disposition: form-data; name="${name}"${CRLF}${CRLF}${value}${CRLF}`))
  textField('chat_id', chatId)
  if (replyToId != null) textField('reply_to_message_id', String(replyToId))
  parts.push(Buffer.from(`--${boundary}${CRLF}Content-Disposition: form-data; name="${field}"; filename="${basename(filePath)}"${CRLF}Content-Type: application/octet-stream${CRLF}${CRLF}`))
  parts.push(data)
  parts.push(Buffer.from(`${CRLF}--${boundary}--${CRLF}`))
  const r = await fetch(`${TELEGRAM_FILE_ROOT}/bot${TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}` },
    body: Buffer.concat(parts),
  })
  const j = await r.json() as any
  if (!j.ok) throw new Error(`${method} ${r.status}: ${j.description || JSON.stringify(j).slice(0, 200)}`)
  return j.result.message_id
}

// ─── 本机 HTTP 接口鉴权（H-15）────────────────────────────────────────
// 威胁模型：防"同机其他 OS 用户""浏览器里的恶意网页""同机其他进程的误用"。
// 已知局限：不防同一账户下已被诱导执行的进程（它能读进程环境/本机文件），用户态无法完全防住；
// 亦请勿把 channel_dir 放进会被同步的目录（口令文件会随之外泄）。
const API_TOKEN_MIN_LEN = 32

/** 读/建本机 HTTP 口令文件 <channelDir>/.api-token（0600）。
 *  缺失 → 用 open('wx',0o600) 一次成型生成 32 字节随机口令；
 *  已存在但权限非 0600 或内容 <32 字节 → 抛错（fail-closed，调用方据此拒绝启动）。 */
export function ensureApiToken(channelDir: string): string {
  const p = join(channelDir, '.api-token')
  const readOk = (): string | null => {
    let st
    try { st = statSync(p) } catch { return null }
    if ((st.mode & 0o777) !== 0o600) throw new Error(`api_token_insecure: ${p} 权限不是 0600`)
    const tok = readFileSync(p, 'utf8')
    if (tok.length < API_TOKEN_MIN_LEN) throw new Error(`api_token_insecure: ${p} 内容过短`)
    return tok
  }
  const existing = readOk()
  if (existing != null) return existing
  const tok = randomBytes(32).toString('hex')   // 64 字符
  try {
    writeFileSync(p, tok, { flag: 'wx', mode: 0o600 })   // 一次成型，无 chmod 竞态窗口
  } catch (e) {
    if ((e as any)?.code === 'EEXIST') { const t = readOk(); if (t != null) return t }
    throw e
  }
  return tok
}

/** 时序安全比较口令（长度不等直接失败）。 */
function tokenMatches(provided: string | null, expected: string): boolean {
  if (provided == null) return false
  const a = Buffer.from(provided)
  const b = Buffer.from(expected)
  if (a.length !== b.length) return false
  try { return timingSafeEqual(a, b) } catch { return false }
}

/** 从请求头取口令：X-Api-Token / X-Api-Key / Authorization: Bearer <t>。 */
function extractApiToken(req: Request): string | null {
  const h = req.headers
  const x = h.get('x-api-token') || h.get('x-api-key')
  if (x && x.trim()) return x.trim()
  const auth = h.get('authorization')
  if (auth) { const m = /^Bearer\s+(.+)$/i.exec(auth.trim()); if (m) return m[1].trim() }
  return null
}

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]'])
/** 跨源判定：带 Origin 头且其 host:port 与本服务不一致 → 跨源。无 Origin → 视为同源放行。 */
function isCrossOrigin(req: Request, boundPort: number): boolean {
  const origin = req.headers.get('origin')
  if (!origin) return false
  let u: URL
  try { u = new URL(origin) } catch { return true }
  const port = u.port ? Number(u.port) : (u.protocol === 'https:' ? 443 : 80)
  return !(LOOPBACK.has(u.hostname) && port === boundPort)
}

/** 全局前置鉴权（默认拒绝）：除 /health 外一律要口令。判定次序固定：
 *  Origin 跨源 → 403（不读 body）→ 口令缺失/错 → 401 → 写方法非 JSON → 415。 */
function guardApi(req: Request, apiToken: string, boundPort: number): Response | null {
  if (isCrossOrigin(req, boundPort)) return new Response('cross-origin forbidden', { status: 403 })
  const url = new URL(req.url)
  if (url.pathname === '/health') return null
  if (!tokenMatches(extractApiToken(req), apiToken)) return new Response('unauthorized', { status: 401 })
  if (req.method === 'POST' || req.method === 'PUT' || req.method === 'PATCH') {
    const ct = (req.headers.get('content-type') || '').split(';')[0].trim().toLowerCase()
    if (ct !== 'application/json') return new Response('unsupported media type', { status: 415 })
  }
  return null
}

// ─── HTTP server for workers ─────────────────────────────────────────
// 已知 POST 端点集合：非 POST 方法只在这些路径上判 405；未列出路径放行落入兜底 404。
const KNOWN_POST_PATHS = new Set(['/promise', '/send', '/react', '/download', '/add_group_alias', '/peer_inbound', '/party', '/photo_round', '/inject'])

// ─── party-camera API（/party、/photo_round）──────────────────────────
const PARTY_ERROR_MESSAGE: Record<string, string> = {
  bad_request: '请求不合法',
  owner_required: '只有主人能开关聚会模式',
  not_member: '这个群不在你的群列表里',
  feature_off: '群聊功能关着',
  payload_too_large: '请求体过大',
  state_write_failed: '聚会模式没写成（状态文件不变）',
  method_not_allowed: '只接受 POST',
}
function partyError(status: number, error: keyof typeof PARTY_ERROR_MESSAGE, detail?: string): Response {
  return Response.json({ ok: false, error, message: PARTY_ERROR_MESSAGE[error], ...(detail ? { detail } : {}) }, { status })
}

async function handlePartyRequest(req: Request): Promise<Response> {
  let raw: string
  try { raw = await req.text() } catch { return partyError(400, 'bad_request', 'body') }
  if (Buffer.byteLength(raw, 'utf8') > 4096) return partyError(413, 'payload_too_large')
  let body: any
  try { body = JSON.parse(raw) } catch { return partyError(400, 'bad_request', 'body') }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return partyError(400, 'bad_request', 'body')
  for (const k of Object.keys(body)) {
    if (k !== 'chat_id' && k !== 'on' && k !== 'note' && k !== 'message_id') return partyError(400, 'bad_request', 'body')
  }
  const chatId = normalizePartyChatId(body.chat_id)
  if (chatId === null) return partyError(400, 'bad_request', 'chat_id')
  let on: boolean
  if (body.on === true || body.on === 'true') on = true
  else if (body.on === false || body.on === 'false') on = false
  else return partyError(400, 'bad_request', 'on')
  const noteGiven = body.note !== undefined
  const noteCheck = normalizePartyNote(body.note)
  if (!noteCheck.ok) return partyError(400, 'bad_request', 'note')

  if (!featureOn(botFeatures(), 'groups')) return partyError(409, 'feature_off')
  const access = loadAccess()
  if (!Object.hasOwn(access.groups, chatId)) return partyError(403, 'not_member')
  const owner = ownerOf(access)
  const now = nowMsFromEnv()
  const lastSender = lastHumanSenderId.get(chatId)
  const ownerSpokeAt = lastOwnerGroupMsgAtMs.get(chatId)
  const windowOk = owner !== '' && lastSender === owner && ownerWindowOpen(now, ownerSpokeAt, OWNER_GROUP_WINDOW_MS)
  // 触发工具的那条投递件（合并件为全部入站）的真人发送者必须全是 owner；
  // missing/unknown/含非 owner → 403 owner_required（不信任 worker 自报身份）。
  const messageIdRaw = body.message_id == null ? '' : String(body.message_id)
  const itemOk = windowOk && inboundItemSendersAllOwner(inboundHumanSenders, chatId, messageIdRaw, owner)
  if (!windowOk || !itemOk) {
    if (windowOk && !itemOk) process.stderr.write(`dispatcher[${BOT_NAME}]: party_owner_required chat=${chatId} reason=message_id\n`)
    return partyError(403, 'owner_required')
  }

  const path = partyStatePath(PARTY_MODE_DIR, chatId)
  if (on) {
    const current = readPartyState(path, now)
    let next
    if (current.status === 'ok' && current.state) {
      next = refreshPartyState(current.state, noteGiven ? { note: noteCheck.note } : {})
    } else {
      if (current.status !== 'missing') {
        logPartyStateProblem(chatId, current.status)
        deletePartyState(path)
      }
      next = newPartyState(chatId, 'bot', noteCheck.note, now)
    }
    if (!writePartyState(path, next)) return partyError(500, 'state_write_failed')
    process.stderr.write(`dispatcher[${BOT_NAME}]: party_on chat=${chatId} source=${next.source}\n`)
    return Response.json({ ok: true, chat_id: chatId, party: partyStateResponse(next) })
  }
  if (!deletePartyState(path)) return partyError(500, 'state_write_failed')
  process.stderr.write(`dispatcher[${BOT_NAME}]: party_off chat=${chatId} reason=tool\n`)
  return Response.json({ ok: true, chat_id: chatId, party: { on: false } })
}

async function handlePhotoRoundRequest(req: Request): Promise<Response> {
  const bad = (detail: 'body' | 'chat_id' | 'delivery_key') =>
    Response.json({ ok: false, error: 'bad_request', detail }, { status: 400 })
  let raw: string
  try { raw = await req.text() } catch { return bad('body') }
  if (Buffer.byteLength(raw, 'utf8') > 4096) return bad('body')
  let body: any
  try { body = JSON.parse(raw) } catch { return bad('body') }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return bad('body')
  const chatId = typeof body.chat_id === 'string' ? body.chat_id
    : typeof body.chat_id === 'number' && Number.isFinite(body.chat_id) ? String(body.chat_id) : ''
  if (!isValidPhotoChatId(chatId)) return bad('chat_id')
  const deliveryKey = body.delivery_key
  if (typeof deliveryKey !== 'string' || deliveryKey === '' || Array.from(deliveryKey).length > 512) return bad('delivery_key')

  const now = nowMsFromEnv()
  const path = photoStatePath(PHOTO_MODE_DIR, BOT_NAME, chatId)
  const current = readPhotoState(path, now)
  if (current.status !== 'ok' || !current.state) {
    if (current.status !== 'missing') {
      logPhotoStateProblem(chatId, current.status)
      deletePhotoState(path)
    }
    return Response.json({ ok: true, active: false })
  }
  if (deliveryKey === current.state.last_delivery_key) {
    return Response.json({ ok: true, active: true, round: current.state.rounds, expires_at_ms: photoExpiresAtMs(current.state) })
  }
  const next = nextPhotoRound(current.state, deliveryKey, now)
  if (!writePhotoState(path, next)) return Response.json({ ok: false, error: 'state_write_failed' }, { status: 500 })
  return Response.json({ ok: true, active: true, round: next.rounds, expires_at_ms: photoExpiresAtMs(next) })
}

async function handleApiRequest(req: Request, apiToken: string, boundPort: number, now: () => number, ledger: Ledger): Promise<Response> {
  const url = new URL(req.url)
  const denied = guardApi(req, apiToken, boundPort)
  if (denied) return denied
  try {
    // 本机应用层健康信号：最近一次 getUpdates 成功时间的秒级时间戳，供看门狗判活。
    // 免口令放行，但仅 GET：非 GET 判 405。
    if (url.pathname === '/health') {
      if (req.method !== 'GET') return new Response('method not allowed', { status: 405 })
      return Response.json({ ok: true, last_poll_ok: Math.floor(lastPollOk / 1000) })
    }
    if (req.method !== 'POST') {
      // /party 的错误体统一为 JSON；其余已知 POST 路径保持既有纯文本。
      if (url.pathname === '/party') return partyError(405, 'method_not_allowed')
      if (KNOWN_POST_PATHS.has(url.pathname)) return new Response('method not allowed', { status: 405 })
      return new Response('not found', { status: 404 })
    }

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

      // 承诺登记/取消/查询：worker 侧 MCP tool 的底层通道。坏 JSON 的契约是 400，
      // 必须在通用 req.json() 之前接住，否则会变成 500。
      if (url.pathname === '/promise') {
        let promiseBody: unknown
        try { promiseBody = await req.json() } catch { return new Response('bad json', { status: 400 }) }
        try {
          const r = await promise.handle(promiseBody)
          return Response.json(r.body, { status: r.status })
        } catch (e) {
          process.stderr.write(`dispatcher[${BOT_NAME}]: promise_handle_failed reason=${e instanceof Error ? e.name : 'unknown'}\n`)
          return Response.json({ ok: false, error: 'store_failed' }, { status: 500 })
        }
      }

      // party-camera：坏 JSON/超长的 400/413 契约要赶在通用 req.json() 之前接住。
      if (url.pathname === '/party') return await handlePartyRequest(req)
      if (url.pathname === '/photo_round') return await handlePhotoRoundRequest(req)

      let body: any
      try { body = await req.json() } catch (e) {
        // /send 的坏 JSON 契约是 400；其余端点沿用既有 500 行为。
        if (url.pathname === '/send') {
          return Response.json({ ok: false, error: 'bad_request', detail: 'body', message_ids: [], parts: [] }, { status: 400 })
        }
        throw e
      }

      if (url.pathname === '/send') {
        // 入口校验：请求体不是普通对象 / chat_id 不合法 → 400 bad_request
        const badRequest = (detail: 'body' | 'chat_id') =>
          Response.json({ ok: false, error: 'bad_request', detail, message_ids: [], parts: [] }, { status: 400 })
        if (!body || typeof body !== 'object' || Array.isArray(body)) return badRequest('body')
        const chatIdOk = (typeof body.chat_id === 'string' && body.chat_id.length > 0) ||
          (typeof body.chat_id === 'number' && Number.isFinite(body.chat_id))
        if (!chatIdOk) return badRequest('chat_id')
        const chatId = String(body.chat_id)
        // 这一条按同一份开关判（现读一次），发往群而群聊关着 → 整条不发
        const feats = botFeatures()
        if (chatId.startsWith('-') && !featureOn(feats, 'groups')) {
          process.stderr.write(`dispatcher[${BOT_NAME}]: group_feature_off /send chat=${chatId} 群聊功能关着，整条不发\n`)
          return Response.json(GROUP_FEATURE_OFF_BODY, { status: 409 })
        }
        // H-16 幂等：带 batch_id 且命中台账 → 直接回放首次结果（逐字），零发送。
        const batchId = typeof body.batch_id === 'string' && body.batch_id ? body.batch_id : undefined
        if (batchId) {
          const hit = ledgerLookup(ledger, batchId, now())
          if (hit) return new Response(hit.text, { status: hit.status, headers: { 'content-type': 'application/json' } })
        }
        const deadline = now() + SEND_TOTAL_BUDGET_MS   // H-17：整条请求耗时上界
        const text = String(body.text ?? '')
        const replyToNum = body.reply_to != null ? Number(body.reply_to) : undefined
        const replyTo = (replyToNum != null && Number.isFinite(replyToNum)) ? replyToNum : undefined
        let files: string[] = []
        if (Array.isArray(body.files)) files = body.files
        else if (typeof body.files === 'string' && body.files.trim()) {
          try { const p = JSON.parse(body.files); if (Array.isArray(p)) files = p }
          catch { files = [body.files] }
        }
        // 出口兜底：落在生图目录、表情库图片根目录下而对应功能关着的文件不发，其余照发。
        // 只按下标跳过、不改 files（[[图N]] 按下标对应）；回应正文带上没发的文件名。
        const fileDrops = planFileDrops(files, { image: IMAGE_OUTPUT_DIR, stickers: stickerImageRoot() },
          { image: featureOn(feats, 'image'), stickers: featureOn(feats, 'stickers') })
        if (fileDrops.counts.image > 0) process.stderr.write(`dispatcher[${BOT_NAME}]: image_feature_off 生图功能关着，${fileDrops.counts.image} 个文件不发\n`)
        if (fileDrops.counts.stickers > 0) process.stderr.write(`dispatcher[${BOT_NAME}]: sticker_feature_off 表情包功能关着，${fileDrops.counts.stickers} 个文件不发\n`)
        // 出口白名单：files 只认媒体目录内的真实文件。拒绝项按下标跳过（[[图N]] 映射不变），
        // 日志只记原因码，路径原文不进日志。
        let fileDroppedDenied = 0
        const safeFilePaths = new Map<number, string>()
        files.forEach((f, i) => {
          const d = resolveAllowedFile(f, allowedFileRoots(), homedir())
          if (d.ok) { safeFilePaths.set(i, d.path); return }
          fileDrops.dropped.add(i)
          fileDroppedDenied++
          process.stderr.write(`dispatcher[${BOT_NAME}]: file_dropped reason=${d.reason}\n`)
        })
        // 只表示请求要求发语音；这次真发不发还要看语音开关
        const wantVoice = body.as_voice === true || body.as_voice === 'true'
        const voiceFeatureOn = featureOn(feats, 'voice')
        const voiceDropped = wantVoice && !voiceFeatureOn
        if (voiceDropped) process.stderr.write(`dispatcher[${BOT_NAME}]: voice_feature_off 语音功能关着，只发文字\n`)
        const featureNotes: Record<string, unknown> = {}
        if (voiceDropped) featureNotes.voice_dropped = 'feature_off'
        if (fileDrops.names.length > 0) Object.assign(featureNotes, { dropped_files: fileDrops.names, dropped_reason: 'feature_off' })
        if (fileDroppedDenied > 0) featureNotes.file_dropped = fileDroppedDenied
        const asVoice = wantVoice && voiceFeatureOn
        const voiceEmotion = body.voice_emotion != null ? String(body.voice_emotion) : 'NEUTRAL'
        const voiceInstruct = body.voice_instruct != null ? String(body.voice_instruct) : ''
        // 如果 worker 没传 voice_text 但要求发语音，自动从 text 剥掉 @username 作为
        // TTS 源（气泡 text 保留 @ 才能触发对方 bot；语音不念 @xxx_bot 才自然）
        const stripMentions = (s: string) => s.replace(/@[A-Za-z0-9_]+/g, '').replace(/[ \t]{2,}/g, ' ').replace(/[ \t]+\n/g, '\n').trim()
        const voiceTextRaw = body.voice_text != null
          ? String(body.voice_text)
          : (wantVoice ? stripMentions(String(body.text ?? '')) : '')
        const promiseIds = sanitizePromiseIds(body.promise_ids)
        const access = loadAccess()
        const limit = Math.max(1, Math.min(access.textChunkLimit ?? MAX_CHUNK_LIMIT, MAX_CHUNK_LIMIT))
        const mode = access.chunkMode ?? 'length'
        const replyMode = access.replyToMode ?? 'first'
        // [[图N]] 标记：让图片能插在文字气泡中间发。无标记时 plan = 全部文字 + 图片压尾，
        // 与旧行为逐字节一致。展开成 chunk 粒度的发送序列后，文字项照旧走分段/打字延迟/
        // 语音配对，图片项在它标记的位置发出。
        const { plan, cleanText } = buildSendPlan(text, files.length)
        type SendItem = { kind: 'text'; chunk: string } | { kind: 'file'; path: string }
        const items: SendItem[] = []
        for (const it of plan) {
          if (it.kind === 'text') {
            const paragraphs = access.splitOnParagraph
              ? it.text.split('\n\n').map(s => s.trim()).filter(s => s.length > 0)
              : (it.text.trim() ? [it.text] : [])
            for (const p of paragraphs) for (const c of chunkText(p, limit, mode)) items.push({ kind: 'text', chunk: c })
          } else {
            // 用校验返回的 realpath 发送，不用请求里的原始字符串：两者不一致时以校验结果为准。
            const f = safeFilePaths.get(it.index)
            if (f && !fileDrops.dropped.has(it.index)) items.push({ kind: 'file', path: f })
          }
        }
        const voiceChunks: string[] = asVoice && voiceTextRaw
          ? voiceTextRaw.split('\n\n').map(s => s.trim())
          : []
        const sentIds: number[] = []
        const dropped: { index: number; reason: string }[] = []
        let sent = 0        // 成功送达的"计划段"数（一个 item 计 1，语音配对的双发也只计 1）
        const delay = access.splitOnParagraph ? (access.paragraphDelay ?? 0) : 0

        // H-16/H-17：单段发送 —— 引用失败去引用重发一次；返回最终成败与归类原因（绝不回显正文）。
        const sendText = async (text: string, replyParams: { message_id: number } | undefined)
          : Promise<{ ok: true; id: number } | { ok: false; reason: string }> => {
          const attempt = async (opts: any) => {
            try { const m = await bot.api.sendMessage(chatId, text, opts); return { ok: true as const, id: m.message_id } }
            catch (e) { return { ok: false as const, reason: segmentReason(e), err: e } }
          }
          const first = await attempt(replyParams ? { reply_parameters: replyParams, allow_sending_without_reply: true } : {})
          if (first.ok) return first
          if (replyParams && isReplyRefError(first.err)) {
            // H-16 §10：引用目标不存在 → 去掉引用参数重发一次；成功即计入 sent。
            process.stderr.write(`dispatcher[${BOT_NAME}]: send_reply_ref_dropped\n`)
            const second = await attempt({})
            if (second.ok) return second
            return { ok: false, reason: second.reason }
          }
          return { ok: false, reason: first.reason }
        }

        // H-17 §16：把单段处理与"总预算剩余时间"赛跑，到界即判 timeout（不再无限等待）。
        const raceBudget = async <T>(p: Promise<T>, msLeft: number): Promise<{ timedOut: false; v: T } | { timedOut: true }> => {
          if (msLeft <= 0) return { timedOut: true }
          let t: any
          const to = new Promise<{ timedOut: true }>(res => { t = setTimeout(() => res({ timedOut: true }), msLeft) })
          try {
            return await Promise.race([p.then(v => ({ timedOut: false as const, v })), to])
          } finally { clearTimeout(t) }
        }

        // 单段处理（绝不抛）。文件段失败按段跳过；文本/语音段走 sendText（含引用降级）。
        const doFile = async (p: string): Promise<{ ok: boolean; reason?: string; ids: number[] }> => {
          const ext = extname(p).toLowerCase()
          const replyToId = (replyTo != null && replyMode !== 'off') ? replyTo : undefined
          const isPhoto = PHOTO_EXTS.has(ext)
          try {
            const mid = await sendFileBuffered(isPhoto ? 'sendPhoto' : 'sendDocument',
              isPhoto ? 'photo' : 'document', chatId, p, replyToId)
            return { ok: true, ids: [mid] }
          } catch (e) {
            process.stderr.write(`dispatcher[${BOT_NAME}]: 文件段失败，按段跳过(${segmentReason(e)})\n`)
            return { ok: false, reason: segmentReason(e), ids: [] }
          }
        }
        const doText = async (t: string, i: number, shouldReplyTo: boolean): Promise<{ ok: boolean; reason?: string; ids: number[] }> => {
          const replyParams = shouldReplyTo ? { message_id: replyTo! } : undefined
          if (asVoice) {
            const voiceId = access.voiceId
            if (!voiceId) {
              process.stderr.write(`dispatcher[${BOT_NAME}]: as_voice 但 access.json 无 voiceId，降级纯文本\n`)
              const r = await sendText(t, replyParams)
              return r.ok ? { ok: true, ids: [r.id] } : { ok: false, reason: r.reason, ids: [] }
            }
            const hasDual = voiceChunks.length > 0
            const ttsText = hasDual ? (voiceChunks[i] ?? '') : t
            const skipVoice = !ttsText || ttsText.trim() === ''
            const ids: number[] = []
            if (hasDual) { const r = await sendText(t, replyParams); if (r.ok) ids.push(r.id) }
            if (!skipVoice) {
              try {
                const vResp = await fetch('http://127.0.0.1:7788/send_voice', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json', ...voiceBridgeAuthHeaders() },
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
                if (!vResp.ok) throw new Error(`voice-bridge ${vResp.status}`)
                const { message_id } = await vResp.json() as { message_id: number }
                ids.push(message_id)
              } catch (e) {
                process.stderr.write(`dispatcher[${BOT_NAME}]: send_voice 失败(${String(e).slice(0, 120)})，` +
                  (hasDual ? '文本已在，跳过这条语音\n' : '回退为文本发送\n'))
                if (!hasDual) { const r = await sendText(ttsText, replyParams); if (r.ok) ids.push(r.id) }
              }
            }
            return ids.length > 0 ? { ok: true, ids } : { ok: false, reason: 'network', ids: [] }
          }
          const r = await sendText(t, replyParams)
          return r.ok ? { ok: true, ids: [r.id] } : { ok: false, reason: r.reason, ids: [] }
        }

        let textIdx = 0   // 只数文字气泡，语音配对和 reply-to-first 都按它算
        for (let n = 0; n < items.length; n++) {
          if (now() >= deadline) { dropped.push({ index: n, reason: 'timeout' }); continue }   // 预算已尽
          const item = items[n]
          let raced: { timedOut: true } | { timedOut: false; v: { ok: boolean; reason?: string; ids: number[] } }
          if (item.kind === 'file') {
            raced = await raceBudget(doFile(item.path), deadline - now())
          } else {
            const i = textIdx++
            if (n > 0 && delay > 0) {
              void bot.api.sendChatAction(chatId, 'typing').catch(() => {})
              await new Promise(r => setTimeout(r, delay))
            }
            const shouldReplyTo = replyTo != null && replyMode !== 'off' && (replyMode === 'all' || i === 0)
            raced = await raceBudget(doText(item.chunk, i, shouldReplyTo), deadline - now())
          }
          if (raced.timedOut) { dropped.push({ index: n, reason: 'timeout' }); continue }
          if (raced.v.ok) {
            // 出站事实源：第一段成功送出时绑定本轮消费的承诺件（只写一次出站时刻）
            if (sent === 0 && promiseIds.length > 0) markOutbound(CHANNEL_DIR, chatId, promiseIds)
            sent++; sentIds.push(...raced.v.ids)
          } else dropped.push({ index: n, reason: raced.v.reason || 'network' })
        }
        // （旧的"图片统一压尾"循环已并入上方计划循环：无标记时行为不变，仍是压尾）
        // Peer inject (groups only; fire-and-forget). Bypass Telegram's
        // bot-to-bot invisibility by notifying peer dispatchers directly.
        // Peer 看到的"真实内容"优先用 voice_text（as_voice 时 text 是播报稿/占位符，
        // voice_text 才是 TTS 合成前的原文）。否则用 text。
        const peerText = (asVoice && voiceTextRaw) ? voiceTextRaw : cleanText
        // Peer 互推：无论导演模式与否都推。原因——Telegram 平台不投递 bot→bot 消息，
        // peer_inbound 是 bot 发言进入 group_transcript 的唯一通道；director.py 靠 transcript
        // 里的 bot 发言算 heat、判断该谁接、给被选中 bot 提供群内上下文。导演模式下不推 =
        // transcript 永远只有真人消息 = director 对每条真人消息只 inject 一个 bot 就停（bot 回复
        // 不回流→没有新消息触发下一轮）＋被选中 bot 看不到别人说了啥（像失忆）。
        // 抢答风险：导演模式下 gate 会 drop 掉 synthetic peer 消息（不路由给 worker），所以 peer
        // 推送只喂 transcript、不会引发 bot 互相回复，安全。
        // 一段都没发出去的话在群里不存在，不推给其他 bot。
        if (sentIds.length > 0 && String(chatId).startsWith('-') && PEERS.length > 0 && peerText.trim().length > 0) {
          const payload = {
            source_bot: BOT_NAME,
            source_username: botUsername,
            source_user_id: sourceBotUserId,
            chat_id: chatId,
            chat_type: 'supergroup',
            text: peerText,
            message_ids: sentIds,
            reply_to: replyTo,
            files: files.filter((_, i) => !fileDrops.dropped.has(i)),   // 功能关着没发的文件不算群里出现过
          }
          for (const peer of PEERS) {
            const ptok = peerApiToken(peer)
            if (!ptok) {
              process.stderr.write(`dispatcher[${BOT_NAME}]: peer inject ${peer.name} skipped: api_token_missing\n`)
              continue
            }
            fetch(`${peer.url}/peer_inbound`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json', 'X-Api-Token': ptok },
              body: JSON.stringify(payload),
            }).catch(e => process.stderr.write(
              `dispatcher[${BOT_NAME}]: peer inject ${peer.name} failed: ${e}\n`))
          }
        }
        // 被晾感知：只有私聊、且真发出去了才武装；生活作息功能关着就不武装
        if (HANG_ON && sentIds.length > 0 && !String(chatId).startsWith('-') && featureOn(feats, 'life')) {
          hangGuard('arm', () => hang.onOutbound(String(chatId)))
        }
        // H-16/H-17：恒含 planned/sent/dropped/idempotent；部分送达=2xx，全丢=502。
        const planned = items.length
        const status = (sent === 0 && planned > 0) ? 502 : 200
        const respText = JSON.stringify({ ok: sent > 0, planned, sent, dropped, idempotent: !!batchId, message_ids: sentIds, ts: now(), ...featureNotes })
        if (batchId) ledgerStore(ledger, batchId, status, respText, now())
        return new Response(respText, { status, headers: { 'content-type': 'application/json' } })
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
        // 群聊关着 → 409，access.json 一个字节不动
        if (!featureOn(botFeatures(), 'groups')) {
          process.stderr.write(`dispatcher[${BOT_NAME}]: group_feature_off /add_group_alias 群聊功能关着，不改 access.json\n`)
          return Response.json(GROUP_FEATURE_OFF_BODY, { status: 409 })
        }
        const groupId = String(body.group_id)
        const aliasRaw = String(body.alias ?? '').trim()
        const kind = String(body.kind)
        if (!aliasRaw || aliasRaw.length > 20) return new Response('bad alias', { status: 400 })
        if (kind !== 'self' && kind !== 'other') return new Response('bad kind', { status: 400 })
        if (!/^-?\d+$/.test(groupId)) return new Response('bad group_id', { status: 400 })
        const raw = readFileSync(ACCESS_FILE, 'utf8')
        const parsed = JSON.parse(raw)
        parsed.groups = parsed.groups ?? {}
        // 新发现的群默认要求 @ 才回，避免建群即对所有人自动应答；allowFrom 仍为空，
        // 配合 senderAllowed 的空名单语义（回退 owner，owner 也空则全拒）。
        parsed.groups[groupId] = parsed.groups[groupId] ?? { requireMention: true, allowFrom: [] }
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
        // 没有消息编号的互推件（对方一段都没发出去）直接丢弃：内存去重与群记录抢占都靠它。
        const sourceMsgId = Number(b.source_message_id)
        const firstListedId = Array.isArray(b.message_ids) && b.message_ids.length > 0 ? Number(b.message_ids[0]) : NaN
        const peerMsgId = Number.isFinite(sourceMsgId) && sourceMsgId > 0
          ? sourceMsgId
          : (Number.isFinite(firstListedId) && firstListedId > 0 ? firstListedId : null)
        if (peerMsgId == null) {
          process.stderr.write(`dispatcher[${BOT_NAME}]: peer_inbound_dropped from=${String(b.source_bot ?? '-').replace(/\s+/g, '_').slice(0, 40)} chat=${chatIdStr} reason=no_message_id\n`)
          return Response.json({ ok: false, error: 'no_message_id' }, { status: 400 })
        }
        const syntheticCtx = {
          chat: {
            id: Number(b.chat_id),
            type: typeof b.chat_type === 'string' && b.chat_type !== ''
              ? b.chat_type
              : (Number(b.chat_id) > 0 ? 'private' : 'supergroup'),
          },
          from: {
            id: Number(b.source_user_id ?? 0),
            is_bot: true,
            username: typeof b.source_username === 'string' ? b.source_username : undefined,
          },
          message: {
            message_id: peerMsgId,
            text: String(b.text ?? ''),
            date: Math.floor(Date.now() / 1000),
            reply_to_message: b.reply_to != null ? { message_id: Number(b.reply_to) } : undefined,
            entities: [],
          },
        } as unknown as Context
        const firstImage = Array.isArray(b.files)
          ? b.files.find((f: any) => typeof f === 'string' && /\.(png|jpe?g|webp|gif)$/i.test(f))
          : undefined
        // 日志脱敏：只记来源/聊天号/消息号，不落用户正文
        process.stderr.write(`dispatcher[${BOT_NAME}]: peer_inbound from=${b.source_bot} chat=${chatIdStr} msgId=${peerMsgId}\n`)
        await handleInbound(syntheticCtx, String(b.text ?? ''), firstImage, undefined, undefined, { synthetic: true })
        return Response.json({ ok: true })
      }

      return new Response('not found', { status: 404 })
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      process.stderr.write(`dispatcher HTTP: ${msg}\n`)
      // 429 单独成码：AI 拿到 500 会当"发送失败"重发，拿到明确的 rate_limited
      // 才知道"消息没发出去、但重发只会更糟，等 N 秒"。
      if (e instanceof RateLimited) {
        return Response.json(
          { ok: false, error: 'rate_limited', retry_after: e.retryAfterSec,
            detail: `Telegram 限流，${e.retryAfterSec} 秒后才能再发。不要重试这条，等冷却结束。` },
          { status: 429 })
      }
      return new Response(msg, { status: 500 })
    }
}

/** 起一个本机 HTTP 接口服务（H-15）。port:0 由系统分配；口令文件固定 <channelDir>/.api-token。
 *  口令缺失/权限不符/过短 → ensureApiToken 抛错，拒绝启动（fail-closed）。
 *  now 可选注入（测试用假时钟驱动 /send 幂等台账 TTL）。 */
export function createApiServer(
  opts: { port?: number; channelDir?: string; now?: () => number } = {},
): { port: number; server: any; stop: (force?: boolean) => void } {
  const channelDir = opts.channelDir ?? CHANNEL_DIR
  const token = ensureApiToken(channelDir)
  const now = opts.now ?? (() => Date.now())
  const ledger: Ledger = { map: new Map() }
  const wanted = opts.port ?? DISPATCHER_PORT
  let boundPort = wanted
  const server = Bun.serve({
    port: wanted,
    hostname: '127.0.0.1',
    fetch: (req: Request) => handleApiRequest(req, token, boundPort, now, ledger),
  })
  boundPort = server.port
  return { port: server.port, server, stop: (force?: boolean) => { try { server.stop(force) } catch {} } }
}

const server = createApiServer({ port: DISPATCHER_PORT })
process.stderr.write(`dispatcher[${BOT_NAME}]: HTTP listening on 127.0.0.1:${server.port}\n`)

// ─── start polling (with 409 retry) ───────────────────────────────────
void (async () => {
  for (let attempt = 1; ; attempt++) {
    try {
      await bot.start({
        // 显式声明收哪些更新：不传会沿用同一账号上的既有设置，拿不到 my_chat_member 就发现不了被拉进的群。
        allowed_updates: ['message', 'my_chat_member'],
        onStart: info => {
          botUsername = info.username
          sourceBotUserId = info.id
          writeTelegramMe(info)
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
