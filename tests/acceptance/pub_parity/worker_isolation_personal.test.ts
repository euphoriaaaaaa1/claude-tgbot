// pub-parity · prepareWorkerIsolation 第 5 步补充（接口文件「补充（2026-10-24 真调后加）」+ 行为清单 26）
// 个人技能 / 个人命令的名字写进 flag 的 skillOverrides（值 'off'），与三个自带项合在一起按字典序写出，同输入两次准备逐字相同。
// 只依据接口文件；只经公开函数 prepareWorkerIsolation 调用；flag 内容从返回的 flagFile 路径读。
// 零副作用：HOME 在 import 前指向临时目录，每个用例有自己的 home 与 bot 目录，不联网、不碰真实 ~/.claude。
// 临时目录建在系统临时目录下（前缀 pp-iso-personal-），本文件不删除任何文件；需要清理时由调用方处理。
// 实现补上这一条之前：凡是期望出现个人名字的用例预期红（skillOverrides 只有三个自带项）；若模块本身不存在，则整个文件加载即红。
import { test, expect } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'

const BASE = mkdtempSync(join(tmpdir(), 'pp-iso-personal-'))
process.env.HOME = join(BASE, 'home')
mkdirSync(process.env.HOME, { recursive: true })

const wi: any = await import('../../../dispatcher/worker_isolation')
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0

type Sandbox = { root: string; home: string; botDir: string; engineDir: string }

// 每个用例一套全新的 home / bot 目录 / 引擎目录。
// engineInHome=true：引擎就放在 <home>/.claude/skills/novelai-skill（行为 26 的原文布局）；否则引擎放在 home 之外。
function sandbox(opts: { engineInHome?: boolean } = {}): Sandbox {
  const root = mkdtempSync(join(BASE, 'case-'))
  const home = join(root, 'home'), botDir = join(root, 'channels', 'bot1')
  const engineDir = opts.engineInHome ? join(home, '.claude', 'skills', 'novelai-skill') : join(root, 'engine', 'novelai-skill')
  mkdirSync(home, { recursive: true }); mkdirSync(join(botDir, '.claude'), { recursive: true }); mkdirSync(engineDir, { recursive: true })
  writeFileSync(join(engineDir, 'SKILL.md'), '---\nname: novelai-skill\n---\n# engine\n')
  return { root, home, botDir, engineDir }
}
const put = (path: string, text = '') => { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, text) }
const userSkills = (sb: Sandbox, ...rel: string[]) => join(sb.home, '.claude', 'skills', ...rel)
const userCommands = (sb: Sandbox, ...rel: string[]) => join(sb.home, '.claude', 'commands', ...rel)
const addSkill = (sb: Sandbox, dir: string, text = '# skill\n') => put(userSkills(sb, dir, 'SKILL.md'), text)

const layer = () => ({ data: {}, unreadable: false })
function prepare(sb: Sandbox): any {
  const r = wi.prepareWorkerIsolation({ botDir: sb.botDir, home: sb.home, user: layer(), engineDir: sb.engineDir })
  expect({ status: r.status, reason: r.reason }).toEqual({ status: 'ok', reason: undefined })   // 前置条件：准备成功，才谈得上看 flag
  return r
}
const flagText = (r: any): string => readFileSync(r.flagFile, 'utf8')
const overridesOf = (r: any): Record<string, unknown> => JSON.parse(flagText(r)).skillOverrides
const off = (...names: string[]): Record<string, string> => Object.fromEntries(names.map(n => [n, 'off']))
const BUNDLED = ['design', 'doctor', 'plugin-authoring']

// 行为 26 的原文布局（引擎 <home>/.claude/skills/novelai-skill 由 sandbox({ engineInHome: true }) 建好）
function layout26(sb: Sandbox) {
  addSkill(sb, 'pp-a', '---\nname: pp-a2\n---\n# pp-a\n')
  put(userCommands(sb, 'pp-c.md'), '# pp-c\n')
  put(userCommands(sb, 'sub', 'pp-d.md'), '# pp-d\n')
  addSkill(sb, 'pp-b')
  put(join(sb.botDir, '.claude', 'skills', 'pp-b', 'SKILL.md'), '# bot 自己的 pp-b\n')
}
const KEYS_26 = ['design', 'doctor', 'plugin-authoring', 'pp-a', 'pp-a2', 'pp-c', 'pp-d']

// ───────── 行为清单 26（三句话三条用例）─────────
test('行为26_综合场景_skillOverrides恰为七项_值都是off_键按字典序_没有novelai-skill与pp-b', () => {
  const sb = sandbox({ engineInHome: true }); layout26(sb)
  const o = overridesOf(prepare(sb))
  expect(o).toEqual(off(...KEYS_26))
  expect(Object.keys(o)).toEqual(KEYS_26)
})
test('行为26_用户级没有skills与commands目录_只有三个自带项_不降级_不新增note', () => {
  const sb = sandbox(); const r = prepare(sb); const o = overridesOf(r)
  expect(o).toEqual(off(...BUNDLED)); expect(Object.keys(o)).toEqual(BUNDLED)
  expect(r.notes).toEqual([])
})
test('行为26_同样输入再准备一次_flag文件逐字相同_个人名字仍在', () => {
  const sb = sandbox({ engineInHome: true }); layout26(sb)
  const r1 = prepare(sb); const text1 = flagText(r1)
  const r2 = prepare(sb)
  expect(Object.keys(overridesOf(r1))).toEqual(KEYS_26)   // 非空转：第一次确实写进了个人名字
  expect(r2.flagFile).toBe(r1.flagFile)
  expect(flagText(r2)).toBe(text1)
})

// ───────── 个人技能 ─────────
test('个人技能_frontmatter里name不在第一行且与目录名不同_目录名与name值都写入', () => {
  const sb = sandbox()
  addSkill(sb, 'pp-a', '---\ndescription: personal skill a\nname: pp-a2\nallowed-tools: Read\n---\n\n# pp-a\nbody\n')
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, 'pp-a', 'pp-a2'))
})
test('个人技能_name写在frontmatter之外_不写入_只写目录名', () => {
  const sb = sandbox()
  addSkill(sb, 'body-name', '---\ndescription: d\n---\nname: ghost-body\n')   // name 行在结束线之后的正文里
  addSkill(sb, 'no-fm', '# 没有 frontmatter\n\nname: ghost-nofm\n')           // 首行不是 ---
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, 'body-name', 'no-fm'))
})
test('个人技能_没有普通文件SKILL_md的条目_不写入_合格的照写', () => {
  const sb = sandbox()
  addSkill(sb, 'pp-ok')
  mkdirSync(userSkills(sb, 'empty-dir'), { recursive: true })                  // 空目录
  put(userSkills(sb, 'readme-only', 'README.md'), '# 只有说明\n')               // 没有 SKILL.md
  put(userSkills(sb, 'skillmd-is-dir', 'SKILL.md', 'inner.txt'), 'x')           // SKILL.md 是目录不是普通文件
  put(userSkills(sb, 'loose-file.md'), '# 散落文件\n')                          // skills 下直接放的文件，不是子目录
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, 'pp-ok'))
})

// ───────── 个人命令 ─────────
test('个人命令_根目录与任意深度子目录的md文件_去掉扩展名后写入_目录名不写入', () => {
  const sb = sandbox()
  put(userCommands(sb, 'pp-c.md'), '# c\n'); put(userCommands(sb, 'sub', 'pp-d.md'), '# d\n'); put(userCommands(sb, 'sub', 'deep', 'pp-e.md'), '# e\n')
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, 'pp-c', 'pp-d', 'pp-e'))
})
test('个人命令_非md文件_不写入_md文件照写', () => {
  const sb = sandbox()
  put(userCommands(sb, 'pp-cmd.md'), '# cmd\n')
  put(userCommands(sb, 'readme.txt'), 'x'); put(userCommands(sb, 'NOTES'), 'x')
  put(userCommands(sb, 'sub', 'old-cmd.md.bak'), 'x'); put(userCommands(sb, 'sub', 'data.json'), '{}')
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, 'pp-cmd'))
})

// ───────── 不写入的名字 ─────────
test('引擎novelai-skill_用户级技能目录里有同名技能_不写入_其他个人技能照写', () => {
  const sb = sandbox()   // 引擎目录在 home 之外，用户级 skills 里另有一份同名技能
  addSkill(sb, 'novelai-skill', '---\nname: novelai-skill\n---\n# 用户级副本\n'); addSkill(sb, 'pp-other')
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, 'pp-other'))
})
test('bot自己的技能目录有同名子目录_不写入_包括只有空目录的情形_用户级其他名字照写', () => {
  const sb = sandbox()
  addSkill(sb, 'pp-b'); addSkill(sb, 'pp-e1'); addSkill(sb, 'pp-keep')
  put(join(sb.botDir, '.claude', 'skills', 'pp-b', 'SKILL.md'), '# bot 的 pp-b\n')     // bot 自己有完整技能
  mkdirSync(join(sb.botDir, '.claude', 'skills', 'pp-e1'), { recursive: true })         // bot 侧只有同名空目录
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, 'pp-keep'))
})
test('个人子代理目录agents_不处理_个人技能照写', () => {
  const sb = sandbox()
  addSkill(sb, 'pp-skill')
  put(join(sb.home, '.claude', 'agents', 'pp-agent.md'), '# agent\n'); put(join(sb.home, '.claude', 'agents', 'sub', 'pp-agent2.md'), '# agent2\n')
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, 'pp-skill'))
})

// ───────── 目录读不出 ─────────
test('读不出_skills与commands路径是普通文件_只有三个自带项_不降级_不新增note', () => {
  const sb = sandbox()
  put(join(sb.home, '.claude', 'skills'), 'not a directory'); put(join(sb.home, '.claude', 'commands'), 'not a directory')
  const r = prepare(sb); const o = overridesOf(r)
  expect(o).toEqual(off(...BUNDLED)); expect(Object.keys(o)).toEqual(BUNDLED); expect(r.notes).toEqual([])
})
test.skipIf(isRoot)('读不出_skills与commands目录无读权限_只有三个自带项_不降级_不新增note', () => {
  const sb = sandbox()
  addSkill(sb, 'pp-hidden'); put(userCommands(sb, 'pp-hidden-cmd.md'), '# c\n')
  const dirs = [userSkills(sb), userCommands(sb)]
  for (const d of dirs) chmodSync(d, 0)
  try {
    const r = prepare(sb); const o = overridesOf(r)
    expect(o).toEqual(off(...BUNDLED)); expect(Object.keys(o)).toEqual(BUNDLED); expect(r.notes).toEqual([])
  } finally { for (const d of dirs) chmodSync(d, 0o755) }
})

// ───────── 名字原样 / 排序 ─────────
test('个人名字含中文_原样写入_目录名与命令文件名都不被改写', () => {
  const sb = sandbox()
  addSkill(sb, '写作助手'); put(userCommands(sb, '翻译.md'), '# 翻译\n'); put(userCommands(sb, '中文目录', '润色.md'), '# 润色\n')
  expect(overridesOf(prepare(sb))).toEqual(off(...BUNDLED, '写作助手', '翻译', '润色'))
})
test('键按字典序_个人名字与三个自带项合在一起排序_技能目录名_frontmatter名_命令名混排', () => {
  const sb = sandbox()
  addSkill(sb, 'zeta-skill', '---\nname: alpha-name\n---\n# z\n')
  put(userCommands(sb, 'dev-cmd.md'), '# dev\n')
  expect(Object.keys(overridesOf(prepare(sb)))).toEqual(['alpha-name', 'design', 'dev-cmd', 'doctor', 'plugin-authoring', 'zeta-skill'])
})
