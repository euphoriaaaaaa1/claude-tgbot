// pub-nai-1121 · 行为 2：隔离 worker 的共享入口链接指向哪里
// 不设 NOVELAI_SKILL_ROOT：指向 <仓库>/skills/novelai-skill；设了就指向设的值。
// 观察方式沿用 pub_parity 的 WorkerManager 拉起夹具思路：假 spawn（不起真 claude）、HOME 与 bot 目录都在临时目录、令牌是假值、不联网。
// 每个场景单独起一个 `bun test` 子进程跑拉起脚本（环境变量在进程启动时定好，互不污染），这里读它写出的结果文件。
import { test, expect, afterAll } from 'bun:test'
import { chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..', '..', '..')
const BASE = mkdtempSync(join(realpathSync(tmpdir()), 'pn-link-'))
afterAll(() => { try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } })

// 子进程里跑的"拉起一次 worker"脚本（作为 .test.ts 跑，才有 mock.module 兜住 node:child_process）
const RUNNER = `
import { test, mock } from 'bun:test'
import * as realCp from 'node:child_process'
import { lstatSync, readlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { FakeChild } from ${JSON.stringify(join(REPO, 'tests', 'acceptance', 'pub_parity', 'wm_harness.ts'))}
test('spawn once', async () => {
  const fakeSpawn = (..._a: any[]) => new FakeChild(2_000_000_001)
  const cpMock = () => ({ ...realCp, spawn: fakeSpawn, default: { ...realCp, spawn: fakeSpawn } })
  mock.module('node:child_process', cpMock); mock.module('child_process', cpMock)
  const mod: any = await import(${JSON.stringify(join(REPO, 'dispatcher', 'worker-manager'))})
  let err: string | null = null
  try { await new mod.WorkerManager({ spawn: fakeSpawn }).spawnWorker() } catch (e: any) { err = String(e?.message ?? e) }
  const link = join(process.env.HOME as string, '.claude', 'worker-shared', '.claude', 'skills', 'novelai-skill')
  let isLink = false
  try { isLink = lstatSync(link).isSymbolicLink() } catch {}
  writeFileSync(process.env.PN_OUT as string, JSON.stringify({ err, isLink, link }))
}, 60000)
`

let seq = 0
async function launchOnce(skillRoot: string | null) {
  const root = join(BASE, `t${++seq}`)
  const home = join(root, 'home'), botDir = join(root, 'channels', 'bot1'), stub = join(root, 'stub')
  for (const d of [home, join(botDir, '.claude'), stub]) mkdirSync(d, { recursive: true })
  for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'schtasks']) {
    writeFileSync(join(stub, n), `#!/bin/sh\nexit 97\n`); chmodSync(join(stub, n), 0o755)   // 万一真去起，也只是立刻退出
  }
  const script = join(root, 'runner.test.ts'); writeFileSync(script, RUNNER)
  const out = join(root, 'result.json')
  const env: Record<string, string> = {
    PATH: `${stub}:${dirname(process.execPath)}:/usr/bin:/bin`, HOME: home, USERPROFILE: home, NO_PROXY: '*', no_proxy: '*',
    CHANNEL_DIR: botDir, BOT_NAME: 'bot1', TELEGRAM_WORKER_BOT: 'bot1', TELEGRAM_WORKER_CHAT_ID: '100001',
    TELEGRAM_DISPATCHER_URL: 'http://127.0.0.1:9', DISPATCHER_URL: 'http://127.0.0.1:9', TELEGRAM_BOT_TOKEN: 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK',
    ANTHROPIC_BASE_URL: 'http://127.0.0.1:9', ANTHROPIC_AUTH_TOKEN: 'FAKE-ANTHROPIC-AUTH-DO-NOT-LEAK',
    BOT_NAMESPACE: '550e8400-e29b-41d4-a716-446655440001', CLAUDEBOTLIFE_TEST: '1', CLAUDEBOTLIFE_TEST_ROOT: root, PN_OUT: out,
  }
  if (skillRoot !== null) env.NOVELAI_SKILL_ROOT = skillRoot
  const proc = Bun.spawn([process.execPath, 'test', script, '--timeout', '60000'], { cwd: root, stdout: 'pipe', stderr: 'pipe', env })
  const [so, se] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()])
  await proc.exited
  expect(existsSync(out) ? 'ok' : `拉起脚本没有写出结果：${(so + se).slice(-800)}`).toBe('ok')
  return JSON.parse(readFileSync(out, 'utf8')) as { err: string | null; isLink: boolean; link: string }
}

test('不设NOVELAI_SKILL_ROOT_共享入口链接指向仓库skills下的novelai-skill', async () => {
  const r = await launchOnce(null)
  expect(r.isLink ? 'link' : `没有建共享入口链接（spawnWorker 报错：${r.err}）`).toBe('link')
  expect(realpathSync(r.link)).toBe(realpathSync(join(REPO, 'skills', 'novelai-skill')))
}, 180000)

test('设了NOVELAI_SKILL_ROOT_共享入口链接指向设的值', async () => {
  const custom = join(BASE, 'custom-root', 'novelai-skill')
  mkdirSync(custom, { recursive: true }); writeFileSync(join(custom, 'SKILL.md'), '# custom\n')
  const r = await launchOnce(custom)
  expect(r.isLink ? 'link' : `没有建共享入口链接（spawnWorker 报错：${r.err}）`).toBe('link')
  expect(realpathSync(r.link)).toBe(realpathSync(custom))
}, 180000)
