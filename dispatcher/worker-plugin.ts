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
import {
  loadInboundState, pickSrcChat, dstActiveMs, crossChatDecision, applyReplyOutcome,
  crossSceneHint, replyToolDescription, testModeCheck,
} from './chat_guard.ts'

const BOT = process.env.TELEGRAM_WORKER_BOT || ''
const DISPATCHER = process.env.TELEGRAM_DISPATCHER_URL || 'http://127.0.0.1:17801'
const CHANNEL_DIR = process.env.CHANNEL_DIR || join(homedir(), '.claude', 'channels', BOT)
// 测试模式 fail-closed（INTERFACE §11.2）：开关未设时直接通过，生产零影响
const testMode = testModeCheck({ ...process.env, CHANNEL_DIR, TELEGRAM_DISPATCHER_URL: DISPATCHER })
if (!testMode.ok) {
  process.stderr.write(`test_mode: refuse ${testMode.reason}\n`)
  process.exit(97)
}
if (!BOT) {
  process.stderr.write(`worker-plugin: need TELEGRAM_WORKER_BOT\n`)
  process.exit(1)
}

// reply 默认回到"刚注入那条消息"的来源 chat（manager 写的 sidecar 文件）
function lastChatId(): string {
  try { return readFileSync(join(CHANNEL_DIR, '.last-chat-id'), 'utf8').trim() } catch { return '' }
}

// desync ④：跨聊天拦截读 worker-manager 原子写的 sidecar；读失败 → 全空 → 不拦（fail-open）
function inboundState() {
  try { return loadInboundState(JSON.parse(readFileSync(join(CHANNEL_DIR, '.last-inbound-ts.json'), 'utf8'))) }
  catch { return loadInboundState(null) }
}
// 主动切换场景的提示文案在 chat_guard.crossSceneHint（纯函数）；这里只读 access.json，读失败 → ''
function readAccessJson(): unknown {
  try { return JSON.parse(readFileSync(join(CHANNEL_DIR, 'access.json'), 'utf8')) } catch { return null }
}
// "去群里说一声"放行账本：进程内，每条用户消息最多放行 1 次；worker 重启即清空（已知残余）
let _group_request_used_ms: Record<string, number> = {}
const CROSS_WARN_PRIV_MS = 30 * 60_000   // 发群时私聊 30 分钟内有真人 → cross_chat_warn（残余风险计量）

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
      crossSceneHint(readAccessJson()),
    ].filter(Boolean).join('\n') },
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
    { name: 'reply', description: '回复消息。默认回到你正在处理的那条消息所在的聊天（群→群、私聊→私聊）；只有要主动跨聊天发时才传 chat_id。text 必填；可选 chat_id、reply_to、files、as_voice、voice_text、voice_emotion、voice_instruct。带 files 时可在 text 中想发图的位置单独一行写 [[图1]]/[[图2]]（对应 files 第几张），图片就插在那两段文字之间发出，像真人聊天；不写标记则图片在所有文字之后发。标记不要写进 voice_text。',
      inputSchema: { type: 'object', properties: {
        text: { type: 'string' }, chat_id: { type: 'string' }, reply_to: { type: 'string' },
        files: { type: 'array', items: { type: 'string' } },
        as_voice: { type: 'boolean' }, voice_text: { type: 'string' },
        voice_emotion: { type: 'string', enum: ['HAPPY','SAD','ANGRY','NEUTRAL','FEARFUL','SURPRISED','DISGUSTED'] },
        voice_instruct: { type: 'string' },
        user_requested: { type: 'boolean', description: replyToolDescription() },
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
        // desync ④：私聊里聊到的内容不主动发进闲置的群；用户在私聊里明确让去群里说 → 放行一次。
        // src/dst 只按真人/导演计时（peer/self-initiate 不算），判定全在 chat_guard 纯函数里。
        const st = inboundState()
        const now = Date.now()
        const src = pickSrcChat(st)
        const srcHumanMs = src != null ? st._human_by_chat[src] ?? null : null
        // D7：放行评估与额度记账只在真正跨聊天（dst≠src）时进行。同一聊天里的回复误带声明 → 当没传，
        // 否则会白白烧掉这条消息唯一一次放行，真要发群时反被判 already_used。
        const userRequested = args.user_requested === true && targetChat !== src
        const decision = crossChatDecision(src, targetChat, dstActiveMs(st, targetChat), now, {
          userRequested,
          srcHumanMs,
          srcMentionsGroup: src != null && st._mentions_group_by_chat[src] === srcHumanMs,
          usedForHumanMs: src != null ? _group_request_used_ms[src] ?? null : null,
        })
        if (decision.block) {
          process.stderr.write(`worker-plugin: cross_chat_block src=${src} dst=${targetChat} idle_min=${decision.idleMin} user_requested=${userRequested ? 1 : 0} denied=${decision.userRequestDenied ?? '-'}\n`)
          const text = decision.userRequestDenied
            ? 'blocked: 不能算用户要求（用户最近没在私聊里让你去群里说，或这条要求已经发过群了）；要说就在私聊里说。'
            : `blocked: 私聊内容不主动发进群（群 ${targetChat} 最近 ${decision.idleMin ?? '未知'} 分钟无人说话）。要说就在私聊里说。`
          return { content: [{ type: 'text', text }] }
        }
        if (decision.bypass === 'user_request') {
          const privMin = srcHumanMs != null ? Math.floor((now - srcHumanMs) / 60_000) : null
          process.stderr.write(`worker-plugin: cross_chat_user_request src=${src} dst=${targetChat} priv_min=${privMin} idle_min=${decision.idleMin}\n`)
        } else if (/^-\d+$/.test(targetChat)) {
          // 放行发群，但私聊 30 分钟内有真人 → 计量"群热闹时带私聊细节"这条只靠人设句管的残余风险。
          // 取最近一个私聊（src 是私聊时就是 src 本身）：群里真人刚说过话时 src 会是群，照样要计。
          let priv: string | null = null
          for (const [c, ms] of Object.entries(st._human_by_chat)) {
            if (/^\d+$/.test(c) && (priv == null || ms > st._human_by_chat[priv])) priv = c
          }
          const privMs = priv != null ? st._human_by_chat[priv] : null
          if (priv != null && privMs != null && now - privMs <= CROSS_WARN_PRIV_MS) {
            process.stderr.write(`worker-plugin: cross_chat_warn src=${priv} dst=${targetChat} priv_min=${Math.floor((now - privMs) / 60_000)} grp_min=${decision.idleMin}\n`)
          }
        }
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
        // 走到这里 = /send 成功（失败抛到外层 catch，不记账）；只有"靠用户要求放行"的那次才记
        _group_request_used_ms = applyReplyOutcome(_group_request_used_ms, src, srcHumanMs, decision, true)
        // dispatcher 回 message_ids；单条形态 message_id 也认（harness mock 与旧 /send 形态）
        const ids: number[] = out.message_ids ?? (out.message_id != null ? [out.message_id] : [])
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
