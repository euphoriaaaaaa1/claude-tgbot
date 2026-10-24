// worker_isolation.ts 单测：补验收用例没覆盖的边界（令牌不外流、链接自愈各分支、排除项、透传类型校验、日志过滤）。
// 零副作用：全部在临时目录里，HOME 指向临时目录，令牌为假值，不起进程、不联网。
import { test, expect, afterAll } from 'bun:test'
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, readlinkSync, rmSync, symlinkSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { dirname, join } from 'path'
import {
  isolationLogLine, overlayEnv, prepareWorkerIsolation, readSettingsFile, summarySpawnPlan, userSettingsPath,
  type SettingsLayer,
} from './worker_isolation'

const BASE = mkdtempSync(join(tmpdir(), 'wi-unit-'))
process.env.HOME = join(BASE, 'home')
afterAll(() => { try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } })

const TOKEN = 'FAKE-TOKEN-unit-0123456789'
let seq = 0
function sandbox(engine = true) {
  const root = join(BASE, `s${++seq}`)
  const home = join(root, 'home'), botDir = join(root, 'channels', 'bot1'), engineDir = join(root, 'engine', 'novelai-skill')
  mkdirSync(join(botDir, '.claude'), { recursive: true }); mkdirSync(home, { recursive: true })
  if (engine) { mkdirSync(engineDir, { recursive: true }); writeFileSync(join(engineDir, 'SKILL.md'), '# e\n') }
  return { root, home, botDir, engineDir, link: join(home, '.claude', 'worker-shared', '.claude', 'skills', 'novelai-skill') }
}
const layer = (data: Record<string, unknown> = {}, unreadable = false): SettingsLayer => ({ data, unreadable })
const prep = (sb: ReturnType<typeof sandbox>, user = layer(), platform?: NodeJS.Platform) =>
  prepareWorkerIsolation({ botDir: sb.botDir, home: sb.home, user, engineDir: sb.engineDir, ...(platform ? { platform } : {}) }) as any

test('令牌不外流：flag 文件、返回值、日志行都不含 env 值', () => {
  const sb = sandbox()
  writeFileSync(join(sb.botDir, '.claude', 'settings.json'), JSON.stringify({ env: { ANTHROPIC_AUTH_TOKEN: TOKEN } }))
  const r = prep(sb, layer({ env: { ANTHROPIC_AUTH_TOKEN: TOKEN, HTTPS_PROXY: `http://u:${TOKEN}@p` }, model: 'm' }))
  expect(r.status).toBe('ok')
  const text = readFileSync(r.flagFile, 'utf8')
  expect(text).not.toContain(TOKEN); expect('env' in JSON.parse(text)).toBe(false)
  expect(JSON.stringify(r)).not.toContain(TOKEN)
  expect(isolationLogLine({ kind: 'prep', info: r })).not.toContain(TOKEN)
})

test('POSIX 指错的链接原子替换为指向引擎，原目标不动', () => {
  const sb = sandbox(); const other = join(sb.root, 'old-engine'); mkdirSync(other); writeFileSync(join(other, 'SKILL.md'), 'old')
  mkdirSync(dirname(sb.link), { recursive: true }); symlinkSync(other, sb.link)
  const r = prep(sb)
  expect(r.status).toBe('ok'); expect(r.notes).toContain('shared_wrong_link')
  expect(readlinkSync(sb.link)).toBe(sb.engineDir); expect(readFileSync(join(other, 'SKILL.md'), 'utf8')).toBe('old')
  expect(readdirSync(dirname(sb.link)).filter(n => n.includes('.tmp-'))).toEqual([])
})

test('悬空链接同样按指错替换', () => {
  const sb = sandbox(); mkdirSync(dirname(sb.link), { recursive: true }); symlinkSync(join(sb.root, 'gone'), sb.link)
  const r = prep(sb)
  expect(r.status).toBe('ok'); expect(r.notes).toContain('shared_wrong_link'); expect(readlinkSync(sb.link)).toBe(sb.engineDir)
})

test('已指向引擎的链接不动、不记 note；win32 下同样认得', () => {
  const sb = sandbox(); prep(sb)
  const ino = lstatSync(sb.link).ino
  for (const p of [undefined, 'win32'] as const) {
    const r = prep(sb, layer(), p)
    expect(r.status).toBe('ok'); expect(r.notes).not.toContain('shared_wrong_link'); expect(lstatSync(sb.link).ino).toBe(ino)
  }
})

test('共享入口是含 SKILL.md 的真实目录：不动、照用，note shared_conflict', () => {
  const sb = sandbox(); mkdirSync(sb.link, { recursive: true }); writeFileSync(join(sb.link, 'SKILL.md'), 'mine')
  const r = prep(sb)
  expect(r.status).toBe('ok'); expect(r.notes).toContain('shared_conflict'); expect(r.sharedDir).toBe(join(sb.home, '.claude', 'worker-shared'))
  expect(lstatSync(sb.link).isDirectory()).toBe(true); expect(readFileSync(join(sb.link, 'SKILL.md'), 'utf8')).toBe('mine')
})

test('共享目录的父路径被文件占住：降级 shared_unavailable，不抛', () => {
  const sb = sandbox(); mkdirSync(join(sb.home, '.claude'), { recursive: true }); writeFileSync(join(sb.home, '.claude', 'worker-shared'), 'x')
  const r = prep(sb)
  expect(r.status).toBe('degraded'); expect(r.reason).toBe('shared_unavailable')
})

test('排除项：含家目录与频道根的 CLAUDE.md，不含 bot 自己的；bot 路径经链接时真实路径也排除', () => {
  const sb = sandbox(); const realRoot = join(sb.root, 'real-channels'); mkdirSync(join(realRoot, 'bot2', '.claude'), { recursive: true })
  symlinkSync(realRoot, join(sb.root, 'linked-channels'))
  const botDir = join(sb.root, 'linked-channels', 'bot2')
  const r: any = prepareWorkerIsolation({ botDir, home: sb.home, user: layer(), engineDir: sb.engineDir })
  const ex: string[] = JSON.parse(readFileSync(r.flagFile, 'utf8')).claudeMdExcludes
  expect(ex).toContain(join(sb.home, '.claude', 'CLAUDE.md'))
  expect(ex).toContain(join(sb.root, 'linked-channels', 'CLAUDE.md'))
  expect(ex).toContain(join(sb.root, 'linked-channels', '.claude', 'rules', '**'))
  expect(ex.some(e => e.endsWith(join('real-channels', 'CLAUDE.local.md')))).toBe(true)
  expect(ex).not.toContain(join(botDir, 'CLAUDE.md'))
  expect(new Set(ex).size).toBe(ex.length)
})

test('透传类型校验：不合整组不写并记 pass_value_invalid；合法布尔照抄', () => {
  const cases: [Record<string, unknown>, string[], string[]][] = [
    [{ effortLevel: 'high', modelSettings: [] }, [], ['effortLevel', 'modelSettings']],
    [{ switchModelsOnFlag: 'yes' }, [], ['switchModelsOnFlag']],
    [{ model: '' }, [], ['model']],
    [{ switchModelsOnFlag: false, alwaysThinkingEnabled: true }, ['switchModelsOnFlag', 'alwaysThinkingEnabled'], []],
  ]
  for (const [user, present, absent] of cases) {
    const r = prep(sandbox(), layer(user)); const f = JSON.parse(readFileSync(r.flagFile, 'utf8'))
    for (const k of present) expect(f[k]).toBe(user[k] as any)
    for (const k of absent) expect(k in f).toBe(false)
    expect(r.notes.includes('pass_value_invalid')).toBe(absent.length > 0)
  }
})

test('用户级 deny 不是数组：不带并记 pass_value_invalid；bot 的 deny 照收、非字符串项丢弃', () => {
  const sb = sandbox(); writeFileSync(join(sb.botDir, '.claude', 'settings.json'), JSON.stringify({ permissions: { deny: ['Read(./x)', 3] } }))
  const r = prep(sb, layer({ permissions: { deny: 'Bash(rm:*)' } }))
  expect(r.notes).toContain('pass_value_invalid')
  expect(JSON.parse(readFileSync(r.flagFile, 'utf8')).permissions).toEqual({ deny: ['Read(./x)'] })
})

test('bot 某份设置坏：note bot_settings_unreadable，按空对象判透传', () => {
  const sb = sandbox(); writeFileSync(join(sb.botDir, '.claude', 'settings.local.json'), '{bad')
  const r = prep(sb, layer({ effortLevel: 'high' }))
  expect(r.status).toBe('ok'); expect(r.notes).toContain('bot_settings_unreadable')
  expect(JSON.parse(readFileSync(r.flagFile, 'utf8')).effortLevel).toBe('high')
})

test('flag 位置被目录占住：降级 flag_write_failed，临时文件清掉；本 pid 的陈旧临时文件不挡下一次写', () => {
  const sb = sandbox(); mkdirSync(join(sb.botDir, '.worker-settings.json'))
  const r = prep(sb)
  expect(r.status).toBe('degraded'); expect(r.reason).toBe('flag_write_failed')
  expect(readdirSync(sb.botDir).filter(n => n.includes('.tmp-'))).toEqual([])
  const sb2 = sandbox(); writeFileSync(join(sb2.botDir, `.worker-settings.json.tmp-${process.pid}`), 'stale')
  expect(prep(sb2).status).toBe('ok')
  expect(existsSync(join(sb2.botDir, `.worker-settings.json.tmp-${process.pid}`))).toBe(false)
})

test('日志行只收常量 note 与 reason，名单外的值不进日志', () => {
  const line = isolationLogLine({ kind: 'prep', info: { status: 'degraded', reason: '/etc/x' as any, notes: ['engine_missing', `/p/${TOKEN}`] } })
  expect(line).toBe('WORKER-ISOLATION mode=on status=degraded reason=prepare_crashed notes=engine_missing')
})

test('readSettingsFile 与 userSettingsPath：相对路径算读不出；CLAUDE_SETTINGS_PATH 优先，否则跟随 env.HOME', () => {
  expect(readSettingsFile('rel/settings.json')).toEqual({ data: {}, unreadable: true })
  expect(userSettingsPath({ CLAUDE_SETTINGS_PATH: '/x/s.json', HOME: '/h' })).toBe('/x/s.json')
  expect(userSettingsPath({ HOME: '/h' })).toBe(join('/h', '.claude', 'settings.json'))
})

test('summarySpawnPlan：全局 env 不覆盖身份键；补 model；关闭标记优先于一切', () => {
  const base = { PATH: '/bin', HOME: '/h' }
  const u = layer({ env: { PATH: '/evil', TELEGRAM_BOT_TOKEN: TOKEN, HTTPS_PROXY: 'http://p' }, model: 'top' })
  const r = summarySpawnPlan(base, u, false)
  expect(r.env).toEqual({ PATH: '/bin', HOME: '/h', HTTPS_PROXY: 'http://p', ANTHROPIC_MODEL: 'top' })
  expect(base).toEqual({ PATH: '/bin', HOME: '/h' })
  expect(summarySpawnPlan(base, u, true).env).toEqual(base)
  expect(overlayEnv({}, { a: 'x', A: 'y' }, ['A'])).toEqual({})
})
