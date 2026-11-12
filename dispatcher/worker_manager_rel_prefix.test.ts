// buildContent 关系状态句：群聊轮次里导演点名（非 bot 发送）也带 relationship.json 的 prompt_snippet，
// 其他 bot 转来的发言不带；两者都仍带【群聊】场景标注。
// 零副作用：HOME/CHANNEL_DIR 全在临时目录，不起进程、不联网。模块级 CHANNEL_DIR 在首次 import 时定死：先设 env 再 import。
import { test, expect, afterAll } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

const BASE = mkdtempSync(join(tmpdir(), 'wm-rel-'))
const BOT_DIR = join(BASE, 'channels', 'bot1')
mkdirSync(BOT_DIR, { recursive: true }); mkdirSync(join(BASE, 'home'), { recursive: true })
Object.assign(process.env, { HOME: join(BASE, 'home'), CHANNEL_DIR: BOT_DIR, BOT_NAME: 'bot1', TELEGRAM_WORKER_BOT: 'bot1',
  TELEGRAM_DISPATCHER_URL: 'http://127.0.0.1:9', TELEGRAM_BOT_TOKEN: 'FAKE-TOKEN-DO-NOT-LEAK', BOT_TZ: 'UTC' })
afterAll(() => { try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } })

const REL_SENTINEL = '[REL-SENTINEL-7f3a] 关系阶段哨兵句'
writeFileSync(join(BOT_DIR, 'relationship.json'), JSON.stringify({ prompt_snippet: REL_SENTINEL }))

const { WorkerManager } = await import('./worker-manager')

function build(meta: Record<string, unknown>): string {
  const m = Object.create(WorkerManager.prototype) as any
  m.inFlight = null; m.phaseVal = 'running'; m.queue = []
  return String(m.buildContent(meta))
}

const GROUP_ID = '-1009876543210'

test('导演点名（群聊、非 bot 发送）以关系状态句开头且带【群聊】', () => {
  const content = build({
    text: '[director] 群里有新消息，轮到你说话了。', chat_id: GROUP_ID, from_id: GROUP_ID,
    from_username: 'director', sender_username: 'director', chat_type: 'group', scene: 'group',
    is_bot_sender: false, ts: '2026-11-12T08:00:00.000Z', message_id: '201',
  })
  expect(content.startsWith(REL_SENTINEL)).toBe(true)
  expect(content).toContain('【群聊】')
})

test('其他 bot 转来的群聊发言不带关系状态句，仍带【群聊】', () => {
  const content = build({
    text: '你好呀', chat_id: GROUP_ID, from_id: '424242',
    from_username: 'peerbot', sender_username: 'peerbot', chat_type: 'group', scene: 'group',
    is_bot_sender: true, ts: '2026-11-12T08:01:00.000Z', message_id: '202',
  })
  expect(content).not.toContain(REL_SENTINEL)
  expect(content).toContain('【群聊】')
})
