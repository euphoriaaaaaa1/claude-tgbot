// WorkerManager × worker 隔离白盒单测：用生产同形的 Node 句柄（on('exit')、stdout.on('data')）驱动启动自检，
// 锁住基础参数全貌，并把 buildContent 输出与入站样例逐字比对（钉死 BOT_TZ，不随运行机时区变）。
// 零副作用：HOME/CHANNEL_DIR/引擎全在临时目录；spawn、kill、计时器全注入，不起进程、不排退避；不联网。
// 模块级 CHANNEL_DIR/BOT 在首次 import 时定死：先设 env 再动态 import。
import { test, expect, afterAll, setSystemTime } from 'bun:test'
import { EventEmitter } from 'events'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

const BASE = mkdtempSync(join(tmpdir(), 'wm-iso-'))
const HOME = join(BASE, 'home'), BOT_DIR = join(BASE, 'channels', 'bot1'), STUB = join(BASE, 'stub')
for (const d of [HOME, join(BOT_DIR, '.claude'), STUB]) mkdirSync(d, { recursive: true })
writeFileSync(join(STUB, 'claude'), '#!/bin/sh\nexit 97\n'); chmodSync(join(STUB, 'claude'), 0o755)
Object.assign(process.env, {
  HOME, CHANNEL_DIR: BOT_DIR, BOT_NAME: 'bot1', BOT_NAMESPACE: '00000000-0000-4000-8000-0000000000aa',   // 测试用假命名空间
  CLAUDE_BIN: join(STUB, 'claude'), TELEGRAM_DISPATCHER_URL: 'http://127.0.0.1:9', TELEGRAM_BOT_TOKEN: 'FAKE-TOKEN-DO-NOT-LEAK',
  NOVELAI_SKILL_ROOT: '', BOT_TZ: 'UTC',
})
delete process.env.CLAUDE_SETTINGS_PATH
afterAll(() => { setSystemTime(); try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } })

const { WorkerManager } = await import('./worker-manager')
const MARK = join(BOT_DIR, '.worker-isolation-off')
const ENGINE = join(HOME, '.claude', 'skills', 'novelai-skill')          // NOVELAI_SKILL_ROOT 为空串 → 默认位置
const LINK = join(HOME, '.claude', 'worker-shared', '.claude', 'skills', 'novelai-skill')
mkdirSync(ENGINE, { recursive: true }); writeFileSync(join(ENGINE, 'SKILL.md'), '# engine\n')

const logs: string[] = []
const realWrite = process.stderr.write.bind(process.stderr)
;(process.stderr as any).write = (c: unknown) => { logs.push(String(c)); return true }
afterAll(() => { (process.stderr as any).write = realWrite })
const isoLines = (from: number) => logs.slice(from).join('').split('\n').map(l => l.slice(Math.max(0, l.indexOf('WORKER-ISOLATION'))))
  .filter(l => l.startsWith('WORKER-ISOLATION'))

type Spawned = { args: string[]; h: any }
function nodeHandle(pid: number) {
  const h: any = new EventEmitter()
  h.pid = pid; h.stdout = new EventEmitter(); h.stderr = new EventEmitter(); h.stdin = { write: () => true }
  return h
}
function newManager() {
  const spawned: Spawned[] = []
  let pid = 2_000_000_100
  const noTimer = { setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {} }
  const m: any = new WorkerManager({
    spawn: (_label: string, spec: any) => { const h = nodeHandle(pid++); spawned.push({ args: [...spec.args], h }); return h },
    kill: () => {}, timer: noTimer,
  })
  return { m, spawned, launch: async () => { await m.spawnWorker(); return spawned[spawned.length - 1] } }
}
const init = (skills: string[]) => Buffer.from(JSON.stringify({ type: 'system', subtype: 'init', skills }) + '\n')
const sources = (s: Spawned) => s.args[s.args.indexOf('--setting-sources') + 1]

test('buildContent 对固定输入的输出与入站样例逐字相同（时区钉成 UTC）', () => {
  setSystemTime(new Date('2026-10-24T08:31:00.000Z'))
  const m = Object.create(WorkerManager.prototype) as any
  m.inFlight = null; m.phaseVal = 'running'; m.queue = []
  const out = String(m.buildContent({ chat_id: '111222333', message_id: 7, from_id: '111222333', from_username: 'alice', user: 'alice',
    scene: 'private', chat_type: 'private', ts: '2026-10-24T08:30:00.000Z', text: '你好，今天天气不错' }))
  setSystemTime()
  expect(out.trimEnd()).toBe(readFileSync(join(import.meta.dir, '..', 'tests', 'fixtures', 'inbound_private.txt'), 'utf8').trimEnd())
})

test('基础参数全貌锁定（开关关时与改动前逐字相同）', async () => {
  writeFileSync(MARK, '')
  try {
    const { m, launch } = newManager()
    const s = await launch()
    expect(s.args).toEqual(['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
      '--session-id', m.sessionUuid, '--dangerously-skip-permissions', '--setting-sources', 'user,project,local',
      '--add-dir', BOT_DIR, '--append-system-prompt-file', join(BOT_DIR, 'CLAUDE.md'),
      '--mcp-config', join(BOT_DIR, 'worker-mcp.json'), '--strict-mcp-config'])
  } finally { rmSync(MARK, { force: true }) }
})

test('NOVELAI_SKILL_ROOT 为空串：按默认位置挂生图技能', async () => {
  const s = await newManager().launch()
  expect(sources(s)).toBe('project,local')
  expect(readlinkSync(LINK)).toBe(ENGINE)
  expect(s.args).toContain(join(HOME, '.claude', 'worker-shared'))
})

test('Node 句柄：init 里有被关掉的技能 → flag_ignored，下一次走旧参数；只看第一个 init', async () => {
  const { launch } = newManager()
  const first = await launch()
  const from = logs.length
  first.h.stdout.emit('data', init(['novelai-skill']))
  first.h.stdout.emit('data', init(['doctor', 'novelai-skill']))          // 同一进程的第二个 init 不再判
  expect(isoLines(from)).toEqual([])
  const second = await launch()
  expect(sources(second)).toBe('project,local')
  second.h.stdout.emit('data', init(['design']))
  expect(isoLines(from)).toContain('WORKER-ISOLATION init_check=fail reason=flag_ignored')
  const third = await launch()
  expect(sources(third)).toBe('user,project,local')
  expect(isoLines(from).at(-1)).toBe('WORKER-ISOLATION mode=on status=degraded reason=flag_ignored notes=-')
})

test('Node 句柄：init 前非 0 退出 → spawn_failed；退出码 0 不置位', async () => {
  const a = newManager()
  ;(await a.launch()).h.emit('exit', 0)
  expect(sources(await a.launch())).toBe('project,local')
  const b = newManager()
  const from = logs.length
  ;(await b.launch()).h.emit('exit', 1)
  expect(isoLines(from)).toContain('WORKER-ISOLATION init_check=fail reason=spawn_failed')
  expect(sources(await b.launch())).toBe('user,project,local')
})

test('dispatcher 主动杀（kill intentional）或 stop() 后的非 0 退出不置位', async () => {
  const a = newManager()
  const ha = (await a.launch()).h
  a.m.kill({ intentional: true }); ha.emit('exit', 143)
  expect(sources(await a.launch())).toBe('project,local')
  const b = newManager()
  const hb = (await b.launch()).h
  const stopping = b.m.stop(); hb.emit('exit', 143); await stopping
  expect(sources(await b.launch())).toBe('project,local')
})

test('init 之后才退出（非 0）不算 spawn_failed', async () => {
  const { launch } = newManager()
  const s = await launch()
  s.h.stdout.emit('data', init(['novelai-skill'])); s.h.emit('exit', 1)
  expect(sources(await launch())).toBe('project,local')
})
