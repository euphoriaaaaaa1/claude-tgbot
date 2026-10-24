// worker_isolation.ts 单测：补验收用例没覆盖的边界（令牌不外流、链接自愈各分支、排除项、透传类型校验、日志过滤、个人技能与命令名）。
// 零副作用：全部在临时目录里，HOME 指向临时目录，令牌为假值，不起进程、不联网。
import { test, expect, afterAll } from 'bun:test'
import { chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, readlinkSync, rmSync, symlinkSync, writeFileSync } from 'fs'
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

// ───────── 个人技能与个人命令写进 skillOverrides（验收没覆盖的边角）─────────
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0
const BUNDLED = ['design', 'doctor', 'plugin-authoring']
const overridesOf = (r: any): Record<string, unknown> => JSON.parse(readFileSync(r.flagFile, 'utf8')).skillOverrides
const off = (...names: string[]) => Object.fromEntries(names.map(n => [n, 'off']))
const putFile = (p: string, text = '') => { mkdirSync(dirname(p), { recursive: true }); writeFileSync(p, text) }
const userSkill = (sb: ReturnType<typeof sandbox>, ...rel: string[]) => join(sb.home, '.claude', 'skills', ...rel)
const userCmd = (sb: ReturnType<typeof sandbox>, ...rel: string[]) => join(sb.home, '.claude', 'commands', ...rel)

test('个人技能：技能目录是链接、SKILL.md 是链接都照写；SKILL.md 是悬空链接的不写', () => {
  const sb = sandbox(); const dot = join(sb.root, 'dotfiles')
  putFile(join(dot, 'linked-skill', 'SKILL.md'), '---\nname: linked-fm\n---\n'); putFile(join(dot, 'only.md'), '# md\n')
  mkdirSync(userSkill(sb, 'md-link'), { recursive: true }); mkdirSync(userSkill(sb, 'dangling'), { recursive: true })
  symlinkSync(join(dot, 'linked-skill'), userSkill(sb, 'linked-dir'))
  symlinkSync(join(dot, 'only.md'), userSkill(sb, 'md-link', 'SKILL.md'))
  symlinkSync(join(sb.root, 'gone.md'), userSkill(sb, 'dangling', 'SKILL.md'))
  expect(overridesOf(prep(sb))).toEqual(off(...BUNDLED, 'linked-dir', 'linked-fm', 'md-link'))
})

test('frontmatter：BOM、CRLF、两端引号与空白都认；没有结束线、name 为空、缩进的 name 都只写目录名', () => {
  const sb = sandbox()
  putFile(userSkill(sb, 'crlf', 'SKILL.md'), '﻿---\r\ndescription: x\r\nname: "crlf-name"\r\n---\r\n# body\r\n')
  putFile(userSkill(sb, 'single', 'SKILL.md'), "---  \nname:   ' single-name '   \n---\n")
  putFile(userSkill(sb, 'unclosed', 'SKILL.md'), '---\nname: ghost-unclosed\n# 没有结束线\n')
  putFile(userSkill(sb, 'empty-name', 'SKILL.md'), '---\nname: ""\n---\n')
  putFile(userSkill(sb, 'nested', 'SKILL.md'), '---\nmeta:\n  name: ghost-nested\n---\n')
  expect(overridesOf(prep(sb))).toEqual(off(...BUNDLED, 'crlf', 'crlf-name', 'single', 'single-name', 'unclosed', 'empty-name', 'nested'))
})

test('skills、commands 各读各的：一个是普通文件读不出，另一个照常写，不降级、不记 note', () => {
  const sb1 = sandbox(); putFile(join(sb1.home, '.claude', 'skills'), 'not a dir'); putFile(userCmd(sb1, 'pp-c.md'))
  const r1 = prep(sb1)
  expect(r1.status).toBe('ok'); expect(r1.notes).toEqual([]); expect(overridesOf(r1)).toEqual(off(...BUNDLED, 'pp-c'))
  const sb2 = sandbox(); putFile(userSkill(sb2, 'pp-s', 'SKILL.md')); putFile(join(sb2.home, '.claude', 'commands'), 'not a dir')
  const r2 = prep(sb2)
  expect(r2.status).toBe('ok'); expect(r2.notes).toEqual([]); expect(overridesOf(r2)).toEqual(off(...BUNDLED, 'pp-s'))
})

test('排除名单对 frontmatter 名与命令名同样适用；bot 侧同名的是链接目录也排除，是普通文件不算', () => {
  const sb = sandbox(); const botSkills = join(sb.botDir, '.claude', 'skills')
  putFile(userSkill(sb, 'pp-x', 'SKILL.md'), '---\nname: novelai-skill\n---\n')
  putFile(userSkill(sb, 'pp-y', 'SKILL.md'), '---\nname: bot-own\n---\n')
  putFile(userCmd(sb, 'novelai-skill.md')); putFile(userCmd(sb, 'sub', 'bot-linked.md')); putFile(userCmd(sb, 'bot-file.md'))
  mkdirSync(join(botSkills, 'bot-own'), { recursive: true }); mkdirSync(join(sb.root, 'shared-skills', 'x'), { recursive: true })
  symlinkSync(join(sb.root, 'shared-skills', 'x'), join(botSkills, 'bot-linked'))
  putFile(join(botSkills, 'bot-file'), 'x')
  expect(overridesOf(prep(sb))).toEqual(off(...BUNDLED, 'pp-x', 'pp-y', 'bot-file'))
})

test('命令：.md 不分大小写；链接文件与链接子目录照收；链接成环不卡死；悬空链接与只剩扩展名的跳过；键按 UTF-16 码元序', () => {
  const sb = sandbox(); const dot = join(sb.root, 'dotfiles')
  putFile(userCmd(sb, 'Upper.MD')); putFile(userCmd(sb, 'mixed.Md')); putFile(userCmd(sb, '.md'))
  putFile(join(dot, 'cmds', 'ext-cmd.md')); putFile(join(dot, 'one.md'))
  symlinkSync(join(dot, 'cmds'), userCmd(sb, 'linked-dir')); symlinkSync(join(dot, 'one.md'), userCmd(sb, 'linked-file.md'))
  symlinkSync(userCmd(sb), userCmd(sb, 'loop')); symlinkSync(join(sb.root, 'gone.md'), userCmd(sb, 'dangling.md'))
  const o = overridesOf(prep(sb))
  expect(o).toEqual(off(...BUNDLED, 'Upper', 'mixed', 'ext-cmd', 'linked-file'))
  expect(Object.keys(o)).toEqual(['Upper', 'design', 'doctor', 'ext-cmd', 'linked-file', 'mixed', 'plugin-authoring'])
})

test.skipIf(isRoot)('局部读不出：SKILL.md 无读权限照写目录名；命令子目录无权限只跳过那一层', () => {
  const sb = sandbox(); const md = userSkill(sb, 'locked-md', 'SKILL.md'); const lockedDir = userCmd(sb, 'locked')
  putFile(md, '---\nname: ghost-locked\n---\n'); putFile(join(lockedDir, 'hidden.md')); putFile(userCmd(sb, 'open.md'))
  chmodSync(md, 0); chmodSync(lockedDir, 0)
  try {
    const r = prep(sb)
    expect(r.status).toBe('ok'); expect(r.notes).toEqual([])
    expect(overridesOf(r)).toEqual(off(...BUNDLED, 'locked-md', 'open'))
  } finally { chmodSync(md, 0o644); chmodSync(lockedDir, 0o755) }
})

test('命令目录深度上限：命令目录本身算第 1 层，第 5 层照收、第 6 层起不进；经链接进去的按链接所在层数接着算', () => {
  const sb = sandbox(); const tree = join(sb.root, 'tree')
  putFile(userCmd(sb, 'l2', 'l3', 'l4', 'l5', 'in5.md')); putFile(userCmd(sb, 'l2', 'l3', 'l4', 'l5', 'l6', 'in6.md'))
  putFile(join(tree, 'a', 'b', 'c', 'in-c.md')); putFile(join(tree, 'a', 'b', 'c', 'd', 'in-d.md'))
  symlinkSync(tree, userCmd(sb, 'deep-link'))   // deep-link 是第 2 层，a、b、c 依次是第 3、4、5 层
  expect(overridesOf(prep(sb))).toEqual(off(...BUNDLED, 'in5', 'in-c'))
})

test('命令目录条目上限：链接指向 2100 个 .md 的大目录，合计只访问 2000 个条目，已收到的照写，不降级、不记 note', () => {
  const sb = sandbox(); const big = join(sb.root, 'big'); mkdirSync(big, { recursive: true })
  for (let i = 0; i < 2100; i++) writeFileSync(join(big, `c${String(i).padStart(4, '0')}.md`), '')
  putFile(userCmd(sb, 'first.md')); symlinkSync(big, userCmd(sb, 'big-link'))
  const r = prep(sb); const o = overridesOf(r)
  expect(r.status).toBe('ok'); expect(r.notes).toEqual([])
  expect(BUNDLED.every(k => o[k] === 'off')).toBe(true)
  // 根目录 2 个条目（first.md、big-link）加大目录里的条目合计 2000 个，其中 big-link 是目录不算名字：不论读取顺序都恰收 1999 个
  expect(Object.keys(o).filter(k => !BUNDLED.includes(k)).length).toBe(1999)
})

test('bot 自己 .claude/commands/ 下的命令名不写入：递归、扩展名不分大小写，对个人技能目录名、frontmatter 名、命令名一律适用；超过深度上限的不算；三个自带项照写', () => {
  const sb = sandbox(); const botCmd = (...rel: string[]) => join(sb.botDir, '.claude', 'commands', ...rel)
  putFile(userCmd(sb, 'pp-shared.md')); putFile(userCmd(sb, 'pp-mine.md')); putFile(userCmd(sb, 'pp-deep.md'))
  putFile(userSkill(sb, 'pp-skill-vs-cmd', 'SKILL.md'), '---\nname: pp-fm-vs-cmd\n---\n'); putFile(userSkill(sb, 'pp-keep', 'SKILL.md'))
  putFile(botCmd('pp-shared.md')); putFile(botCmd('sub', 'pp-skill-vs-cmd.md')); putFile(botCmd('x', 'pp-fm-vs-cmd.MD'))
  putFile(botCmd('l2', 'l3', 'l4', 'l5', 'l6', 'pp-deep.md')); putFile(botCmd('design.md'))
  const r = prep(sb)
  expect(r.status).toBe('ok'); expect(r.notes).toEqual([])
  expect(overridesOf(r)).toEqual(off(...BUNDLED, 'pp-mine', 'pp-deep', 'pp-keep'))
})
