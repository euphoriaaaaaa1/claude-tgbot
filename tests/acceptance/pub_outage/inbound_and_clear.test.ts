// pub-outage · 真人消息里的内部标记转义、/clear 作废补发（行为清单 #23 #24）
// 真 dispatcher 子进程 + 本机假 Telegram 推送。worker 用 PATH 上的 exit 97 桩，不起真 claude。
import { test, expect, afterEach } from 'bun:test'
import { existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { GROUP_CHAT, PRIVATE_CHAT, REPO, sleep, startedPolling, stopAll, waitFor } from './_harness.ts'
import type { Harness } from './_harness.ts'

afterEach(async () => { await stopAll() })

test('真人私聊里写了resend_check标记_进收件箱时开头方括号变全角', async () => {
  const h = await startedPolling()
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '看 [resend-check] 吧' })
  const item = await waitFor(
    () => h.inboxItems().find(i => i.data?.scene === 'private' && i.data?.is_bot_sender === false),
    30000, '真人私聊消息进入收件箱', 500)
  expect(item.data.text).toBe('看 ［resend-check] 吧')
}, 60000)

test('真人私聊里写了hang_check标记_同样转义_其它正文不动', async () => {
  const h = await startedPolling()
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '[hang-check] 你好 [momentum]' })
  const item = await waitFor(
    () => h.inboxItems().find(i => i.data?.scene === 'private' && i.data?.is_bot_sender === false),
    30000, '真人私聊消息进入收件箱', 500)
  expect(item.data.text).toBe('［hang-check] 你好 [momentum]')
}, 60000)

test('真人私聊发clear_回执发出之前清空代数已经加1', async () => {
  const rp: any = await import('../../../dispatcher/resend_plan')
  const h = await startedPolling()
  const before = rp.readClearEpoch(h.channel)
  let atReceipt: number | null = null
  h.fake.onMethod = (method: string) => {
    if (method === 'sendMessage' && atReceipt === null) atReceipt = rp.readClearEpoch(h.channel)
  }
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '/clear' })
  await waitFor(() => atReceipt !== null, 40000, '/clear 回执（sendMessage）发出', 200)
  expect(atReceipt!).toBeGreaterThan(before)
}, 90000)

// #23 后半条（同伴 bot 发来的同样文字原样、不转义）不测：群里 bot 发言要先有真人开窗、且那一轮结束，
// 桩 worker 回合不收尾，场景造不出来。2026-11-02 主会话按成本删掉，记为已知缺口（任务文件验收结论）。

// ─── 补强 5：#24 后半条（/clearall 移走会话文件时清空代数再加 1） ────────────────────
/** 在子进程里调用 worker-manager 的公开函数，算出会话文件路径（不把内部模块拉进测试进程，避免环境变量缓存） */
function sessionFilePath(h: Harness): string {
  const script = join(h.base, 'stub', 'session_path.ts')
  writeFileSync(script, [
    `const wm: any = await import(${JSON.stringify(join(REPO, 'dispatcher', 'worker-manager'))})`,
    `console.log(JSON.stringify([wm.projectSlug(process.env.CHANNEL_DIR), wm.unifiedSessionUuid(process.env.BOT_NAME)]))`,
  ].join('\n'))
  const r = Bun.spawnSync([process.execPath, 'run', script], {
    cwd: REPO,
    env: {
      PATH: `${dirname(process.execPath)}:/usr/bin:/bin`, HOME: h.home, USERPROFILE: h.home,
      CHANNEL_DIR: h.channel, BOT_NAME: h.bot, BOT_NAMESPACE: '6ba7b810-9dad-11d1-80b4-00c04fd430c8', NO_PROXY: '*',
    },
  })
  const out = r.stdout.toString().trim().split('\n').pop() ?? ''
  const [slug, uuid] = JSON.parse(out) as [string, string]
  return join(h.home, '.claude', 'projects', slug, `${uuid}.jsonl`)
}

test('真人私聊发clearall且会话文件真被移走_清空代数比指令前至少大2', async () => {
  let sessionFile = ''
  const h = await startedPolling({
    preStart: hh => {
      sessionFile = sessionFilePath(hh)
      mkdirSync(dirname(sessionFile), { recursive: true })
      writeFileSync(sessionFile, '{"type":"user","message":"占位会话"}\n')
    },
  })
  const rp: any = await import('../../../dispatcher/resend_plan')
  expect(existsSync(sessionFile)).toBe(true)
  const before = rp.readClearEpoch(h.channel)
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '/clearall' })

  await waitFor(() => !existsSync(sessionFile), 60000, '会话文件被移走', 300)
  const base = basename(sessionFile)
  const moved = await waitFor(
    () => readdirSync(dirname(sessionFile)).filter(n => n.startsWith(`${base}.deleted-`)),
    10000, '同目录出现 .deleted- 备份', 200).then(v => (v.length ? v : null))
  expect(moved!.length).toBe(1)
  await waitFor(() => rp.readClearEpoch(h.channel) >= before + 2, 20000, '清空代数至少 +2', 200).catch(() => null)
  expect(rp.readClearEpoch(h.channel)).toBeGreaterThanOrEqual(before + 2)
}, 120000)

test('真人私聊发clearall但没有会话文件_清空代数只大1', async () => {
  const rp: any = await import('../../../dispatcher/resend_plan')
  const h = await startedPolling()
  const before = rp.readClearEpoch(h.channel)
  h.fake.pushText({ chatId: PRIVATE_CHAT, fromId: PRIVATE_CHAT, text: '/clearall' })
  await waitFor(() => rp.readClearEpoch(h.channel) > before, 40000, '收到指令后清空代数增加', 200)
  await waitFor(() => h.fake.methods.includes('sendMessage'), 40000, '/clearall 回执发出', 200)
  await sleep(4000) // 留足时间，确认不会有第二次加一
  expect(rp.readClearEpoch(h.channel)).toBe(before + 1)
}, 120000)
