// H3a-H13 清空类命令鉴权 —— 黑盒验收（bug 修复模式：当前应为红，修好后转绿）
// 只依据 .devflow/INTERFACE-h3a-security.md 二(5-8)。不读实现。
// 只调 authorizeClear(ctx) 并观察：放行/拒绝的判定；拒绝时通道目录零改动 + 回执次数。
import { test, expect, afterAll } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { loadDispatcher, pickFn, allowed, CLEAR_FN_NAMES, OWNER, NONOWNER, GROUP, BOT_USERNAME, CHANNEL_DIR, snapshotDir, cleanupBase } from './_fixtures.ts'

afterAll(cleanupBase)

const dm: any = await loadDispatcher()
const authorizeClear: any = pickFn(dm, CLEAR_FN_NAMES)

function need(): void {
  if (typeof authorizeClear !== 'function') throw new Error(`未发现 authorizeClear 导出（候选名 ${CLEAR_FN_NAMES.join('/')}）`)
}

/** 造一个 ctx；带可选回执计数（覆盖几种常见回执入口）。 */
function mkCtx(o: { chatType: 'private' | 'group'; chatId: string; fromId: string; text: string; count?: { reply: number } }) {
  const ctx: any = {
    chat: { id: o.chatId, type: o.chatType },
    from: { id: o.fromId },
    me: { username: BOT_USERNAME },
    message: { text: o.text },
    msg: { text: o.text },
  }
  if (o.count) {
    const bump = () => { o.count!.reply++ }
    ctx.reply = bump
    ctx.replyWithMarkdown = bump
    ctx.replyWithHTML = bump
    ctx.api = { sendMessage: bump }
  }
  return ctx
}

async function ask(ctx: any): Promise<boolean> {
  return allowed(await authorizeClear(ctx))
}

// ── 契约 6/8：私聊主人放行（既有 /clear 行为不变）──────────────────────────
test('H13_私聊主人_clear_放行', async () => {
  need()
  expect(await ask(mkCtx({ chatType: 'private', chatId: OWNER, fromId: OWNER, text: '/clear' }))).toBe(true)
})

// ── 契约 6：私聊非名单 → 拒绝 ──────────────────────────────────────────────
test('H13_私聊非主人_拒绝', async () => {
  need()
  expect(await ask(mkCtx({ chatType: 'private', chatId: NONOWNER, fromId: NONOWNER, text: '/clear' }))).toBe(false)
})

// ── 契约 6：群里的 id（哪怕进了群名单）一律不算主人 ─────────────────────────
test('H13_群里_即便在群名单_也拒绝', async () => {
  need()
  // 主人 id 出现在群里：也拒绝（群里的成员身份 ≠ 主人）
  expect(await ask(mkCtx({ chatType: 'group', chatId: GROUP, fromId: OWNER, text: '/clear' }))).toBe(false)
  // 群名单里的普通成员：拒绝
  expect(await ask(mkCtx({ chatType: 'group', chatId: GROUP, fromId: '555888', text: '/clearall' }))).toBe(false)
})

// ── 契约 7：@后缀 必须等于本 bot，否则拒绝（不得"忽略后缀继续执行"）──────────
test('H13_后缀是别的bot_拒绝', async () => {
  need()
  expect(await ask(mkCtx({ chatType: 'private', chatId: OWNER, fromId: OWNER, text: '/clear@SomeOtherBot' }))).toBe(false)
})

// ── 契约 7：别名与大小写归一化后仍正确判定 ─────────────────────────────────
test('H13_别名与大小写归一化_私聊主人放行', async () => {
  need()
  const forms = ['/CLEAR', '/clear_all', '/CLEARALL', `/clear@${BOT_USERNAME}`, `/CLEAR@${BOT_USERNAME.toUpperCase()}`]
  for (const text of forms) {
    expect(await ask(mkCtx({ chatType: 'private', chatId: OWNER, fromId: OWNER, text }))).toBe(true)
  }
})

// ── 契约 6：群里带后缀/无后缀都拒绝，且不建/移任何会话文件 ──────────────────
test('H13_群里带后缀与无后缀都拒绝_文件零改动', async () => {
  need()
  const before = snapshotDir(CHANNEL_DIR)
  for (const text of ['/clear', '/clearall', `/clear@${BOT_USERNAME}`]) {
    expect(await ask(mkCtx({ chatType: 'group', chatId: GROUP, fromId: OWNER, text }))).toBe(false)
  }
  expect(snapshotDir(CHANNEL_DIR)).toEqual(before)   // 清空动作零副作用：不新建/移动会话文件
})

// ── 契约 6：拒绝路径回执恰好一次 ───────────────────────────────────────────
test('H13_拒绝_回执恰好一次', async () => {
  need()
  const count = { reply: 0 }
  const ok = await ask(mkCtx({ chatType: 'private', chatId: NONOWNER, fromId: NONOWNER, text: '/clear', count }))
  expect(ok).toBe(false)
  expect(count.reply).toBe(1)                        // 拒绝也要回一条固定文案
})

// ── 反向：通道目录里的 access.json 未被改动 ────────────────────────────────
test('H13_拒绝路径_不改写access名单', async () => {
  need()
  const p = join(CHANNEL_DIR, 'access.json')
  const before = readFileSync(p, 'utf8')
  await ask(mkCtx({ chatType: 'group', chatId: GROUP, fromId: OWNER, text: '/clear' }))
  expect(readFileSync(p, 'utf8')).toBe(before)
})
