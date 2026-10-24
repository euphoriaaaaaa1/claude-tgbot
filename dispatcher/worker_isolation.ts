/**
 * worker_isolation.ts — worker 与 /clear 摘要不再加载用户级设置源（--setting-sources project,local）。
 *
 * 去掉 user 源后，用户级的钩子、插件、个人技能、全局 CLAUDE.md 不再进 bot；为了让"bot 没配的就跟随全局"
 * 的效果不变，这里把需要的部分补回来：
 *   - 全局 settings.json 的 env 段叠回子进程环境（overlayEnv，跳过身份键与 bot 自己已设的键）；
 *   - 透传组（思考强度、模型等）在 bot 两份设置都没设时写进 flag 层 <bot目录>/.worker-settings.json（经 --settings 传入）；
 *   - 全局 permissions.deny 与 bot 的取并集写进 flag 层；
 *   - 生图技能经 ~/.claude/worker-shared/.claude/skills/novelai-skill 链接挂入（--add-dir）。
 * bot 目录在家目录下时，CLI 从 cwd 逐级向上找 .claude/skills、.claude/commands，会把用户级个人技能与个人命令当上级项目的
 * 读进来（--setting-sources 管不到），所以再把这些名字写进 flag 层 skillOverrides 关掉；个人子代理这样关不掉，不处理。
 * 准备失败一律降级成旧参数（参数与环境与改动前逐字相同），日志只拼代码常量。
 * 全部导出函数不抛错、不读模块级环境变量；令牌只许进子进程环境，不进返回值以外的任何地方。
 */

import {
  closeSync, type Dir, lstatSync, mkdirSync, opendirSync, openSync, readdirSync, readFileSync, readlinkSync, readSync, realpathSync,
  renameSync, rmdirSync, statSync, symlinkSync, unlinkSync, writeFileSync,
} from 'fs'
import { dirname, isAbsolute, join, resolve } from 'path'
import { homedir } from 'os'

export const LEGACY_SETTING_SOURCES = 'user,project,local'
export const ISOLATED_SETTING_SOURCES = 'project,local'
export const HELPER_ISOLATION_ARGS = ['--setting-sources', 'project,local', '--strict-mcp-config', '--no-session-persistence']
export const LEGACY_SUMMARY_ARGS = ['--strict-mcp-config', '--no-session-persistence']
export const SWITCH_OFF_FILE = '.worker-isolation-off'   // 放 bot 目录里 = 关这只；放 bot 目录的上一级 = 全局关
export const FLAG_FILE = '.worker-settings.json'
export const SHARED_ENGINE = 'novelai-skill'
export const DISABLED_BUNDLED = ['design', 'doctor', 'plugin-authoring']   // skillOverrides 关掉的三个，兼作自检探针
// 取自 Claude Code 2.1.308 设置描述里会产出凭证或认证头的键
export const AUTH_HELPER_KEYS = ['apiKeyHelper', 'awsAuthRefresh', 'awsCredentialExport', 'gcpAuthRefresh', 'proxyAuthHelper']
export const PASS_GROUPS = [['effortLevel', 'modelSettings'], ['alwaysThinkingEnabled'], ['switchModelsOnFlag'], ['model']]
export const PROTECTED_ENV_PREFIXES = ['TELEGRAM_', 'CLAUDEBOTLIFE_', 'CLAUDEBOT_']
export const PROTECTED_ENV_KEYS = ['PATH', 'HOME', 'CHANNEL_DIR', 'PEERS_JSON']

export type SettingsLayer = { data: Record<string, unknown>; unreadable: boolean }
export type BotLayers = { main: SettingsLayer; local: SettingsLayer }
export type DegradeReason = 'user_auth_helper' | 'shared_unavailable' | 'flag_write_failed' | 'prepare_crashed'
  | 'args_unexpected' | 'flag_ignored' | 'engine_not_loaded' | 'spawn_failed'
export type InitFailReason = 'flag_ignored' | 'engine_not_loaded' | 'spawn_failed'
export type IsolationInfo = { status: 'ok' | 'degraded'; reason?: DegradeReason; notes: string[] }   // 只含常量，可进日志
export type PrepareOk = IsolationInfo & { status: 'ok'; flagFile: string; sharedDir: string | null; botEnvKeys: string[] }
export type PrepareDegraded = IsolationInfo & { status: 'degraded'; reason: DegradeReason }
export type LogInput = { kind: 'off'; src: 'switch' | 'global' } | { kind: 'prep'; info: IsolationInfo }
  | { kind: 'init_fail'; reason: InitFailReason }

// 日志只许出现这些常量（isolationLogLine 按名单过滤，名单外的一律不进日志）
const DEGRADE_REASONS: readonly string[] = ['user_auth_helper', 'shared_unavailable', 'flag_write_failed', 'prepare_crashed',
  'args_unexpected', 'flag_ignored', 'engine_not_loaded', 'spawn_failed']
const INIT_FAIL_REASONS: readonly string[] = ['flag_ignored', 'engine_not_loaded', 'spawn_failed']
const NOTE_KEYS: readonly string[] = ['engine_missing', 'shared_conflict', 'shared_wrong_link', 'bot_settings_unreadable',
  'user_settings_unreadable', 'pass_value_invalid']
const CLAUDE_MD_PATTERNS = ['CLAUDE.md', 'CLAUDE.local.md', join('.claude', 'CLAUDE.md'), join('.claude', 'rules', '**')]

const EMPTY_LAYER = (): SettingsLayer => ({ data: {}, unreadable: false })

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}
function isAbsPath(p: unknown): p is string {
  return typeof p === 'string' && p.length > 0 && isAbsolute(p)
}
function hasKey(o: Record<string, unknown>, k: string): boolean {
  return Object.prototype.hasOwnProperty.call(o, k)
}
// 调用方传来的层可能缺字段：一律收敛成合法形状，后面的判断不必处处防御
function asLayer(x: unknown): SettingsLayer {
  if (!isPlainObject(x)) return EMPTY_LAYER()
  return { data: isPlainObject(x.data) ? x.data : {}, unreadable: x.unreadable === true }
}

/**
 * 用户级设置文件路径：与 provider_watch.settingsPath() 同一份（CLAUDE_SETTINGS_PATH 优先），
 * 但家目录取 env.HOME：bun 的 os.homedir() 是进程启动时的快照，运行中改 HOME 取不到（同 worker-manager 的 sessionJsonlPath）。
 */
export function userSettingsPath(env: Record<string, string | undefined> = process.env): string {
  return env.CLAUDE_SETTINGS_PATH || join(env.HOME || homedir(), '.claude', 'settings.json')
}

export function readSettingsFile(path: string): SettingsLayer {
  if (!isAbsPath(path)) return { data: {}, unreadable: true }
  let raw: string
  try {
    raw = readFileSync(path, 'utf8')
  } catch (e) {
    const code = (e as NodeJS.ErrnoException)?.code
    // 不存在不算坏；EACCES、EISDIR 等其余读错都算读不出
    return { data: {}, unreadable: !(code === 'ENOENT' || code === 'ENOTDIR') }
  }
  try {
    const data = JSON.parse(raw)
    return isPlainObject(data) ? { data, unreadable: false } : { data: {}, unreadable: true }
  } catch {
    return { data: {}, unreadable: true }
  }
}

/** 全局 env 段里值为字符串的键。返回值可能含令牌：只许交给子进程环境。 */
export function globalSettingsEnv(user: SettingsLayer): Record<string, string> {
  const u = asLayer(user)
  const out: Record<string, string> = {}
  if (u.unreadable || !isPlainObject(u.data.env)) return out
  for (const [k, v] of Object.entries(u.data.env)) if (typeof v === 'string') out[k] = v
  return out
}

export function overlayEnv(target: Record<string, string>, extra: Record<string, string>, skipKeys: string[] = []): Record<string, string> {
  const out: Record<string, string> = { ...target }
  const skip = new Set([...PROTECTED_ENV_KEYS, ...(Array.isArray(skipKeys) ? skipKeys : [])].map(k => String(k).toUpperCase()))
  for (const [k, v] of Object.entries(isPlainObject(extra) ? extra : {})) {
    if (typeof v !== 'string') continue
    const up = k.toUpperCase()
    if (skip.has(up) || PROTECTED_ENV_PREFIXES.some(p => up.startsWith(p))) continue
    out[k] = v
  }
  return out
}

export function hasUserAuthHelper(user: SettingsLayer, bot?: BotLayers): boolean {
  const u = asLayer(user)
  if (u.unreadable) return false
  const main = bot ? asLayer(bot.main).data : {}
  const local = bot ? asLayer(bot.local).data : {}
  return AUTH_HELPER_KEYS.some(k => {
    if (!hasKey(u.data, k)) return false
    const v = u.data[k]
    if (v === null || v === undefined || v === '' || v === false) return false
    return !(hasKey(main, k) || hasKey(local, k))
  })
}

function lstatOrNull(p: string): ReturnType<typeof lstatSync> | null {
  try { return lstatSync(p) } catch { return null }
}

export function isolationSwitchOff(botDir: string): 'switch' | 'global' | null {
  if (!isAbsPath(botDir)) return null
  if (lstatOrNull(join(botDir, SWITCH_OFF_FILE))) return 'switch'
  const parent = dirname(botDir)
  if (parent === botDir) return null                        // 根目录没有上一级，不判全局
  return lstatOrNull(join(parent, SWITCH_OFF_FILE)) ? 'global' : null
}

function isRegularFile(p: string): boolean {
  try { return statSync(p).isFile() } catch { return false }
}

// 链接目标是否就是引擎目录。Windows 的联接 readlink 可能带 \\?\ 前缀、大小写不定、分隔符混用。
function sameLinkTarget(cur: string, engineDir: string, linkParent: string, win: boolean): boolean {
  if (win) {
    const norm = (p: string) => p.replace(/^\\\\\?\\/, '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase()
    return norm(cur) === norm(engineDir)
  }
  return resolve(linkParent, cur) === resolve(engineDir)
}

// 只删链接本身：Windows 联接用非递归 rmdir（碰到真实目录必然失败，碰不到目标里的文件）；
// 非 Windows 宿主上注入 win32 测试时 rmdir 对符号链接报 ENOTDIR，退回 unlink（同样只删链接）。
function removeLinkOnly(link: string): void {
  try {
    rmdirSync(link)
  } catch (e) {
    if ((e as NodeJS.ErrnoException)?.code !== 'ENOTDIR') throw e
    unlinkSync(link)
  }
}

// 在 link 处建指向 engineDir 的链接。POSIX：本 pid 临时名建好后 rename 到位（原子，可覆盖已有链接）；
// Windows：rename 不能覆盖已有链接，调用方须先删旧联接，这里直接建 junction（不需要管理员权限）。
function placeLink(link: string, engineDir: string, win: boolean): void {
  if (win) {
    try {
      symlinkSync(engineDir, link, 'junction')
    } catch (e) {
      if ((e as NodeJS.ErrnoException)?.code !== 'EEXIST') throw e
      // 别的进程刚建好：重新判一次，是指向引擎的链接就算成功，否则按失败处理
      const st = lstatOrNull(link)
      if (!st?.isSymbolicLink() || !sameLinkTarget(readlinkSync(link), engineDir, dirname(link), true)) throw e
    }
    return
  }
  const tmp = `${link}.tmp-${process.pid}`
  try {
    try { unlinkSync(tmp) } catch { /* 没有本 pid 的残留就算了 */ }
    symlinkSync(engineDir, tmp)
    renameSync(tmp, link)
  } catch (e) {
    try { unlinkSync(tmp) } catch { /* ignore */ }
    throw e
  }
}

// 共享入口自愈。返回 null = 共享目录不可用（调用方降级 shared_unavailable）。
function ensureSharedEngine(home: string, engineDir: string, win: boolean, addNote: (n: string) => void): string | null {
  const sharedDir = join(home, '.claude', 'worker-shared')
  const link = join(sharedDir, '.claude', 'skills', SHARED_ENGINE)
  try {
    mkdirSync(dirname(link), { recursive: true })
    const st = lstatOrNull(link)
    if (st === null) {
      placeLink(link, engineDir, win)
    } else if (st.isSymbolicLink()) {
      let cur: string | null = null
      try { cur = readlinkSync(link) } catch { /* 读不出按指错处理 */ }
      if (cur === null || !sameLinkTarget(cur, engineDir, dirname(link), win)) {
        addNote('shared_wrong_link')
        if (win) removeLinkOnly(link)
        placeLink(link, engineDir, win)
      }
    } else {
      addNote('shared_conflict')                              // 用户自己放的真实目录或文件：不动，有 SKILL.md 就照用
    }
  } catch {
    return null
  }
  return isRegularFile(join(link, 'SKILL.md')) ? sharedDir : null
}

// 目录 d 的父目录逐级到根（不含 d 本身）
function ancestors(d: string): string[] {
  const out: string[] = []
  let cur = dirname(d)
  for (;;) {
    out.push(cur)
    const up = dirname(cur)
    if (up === cur) break
    cur = up
  }
  return out
}

// bot 目录与共享目录各自的祖先链，字面路径与解析链接后的真实路径各走一遍；Windows 每项写两种分隔符
function claudeMdExcludes(dirs: string[], win: boolean): string[] {
  const seen = new Set<string>()
  const add = (p: string) => {
    if (win) { seen.add(p.replace(/\//g, '\\')); seen.add(p.replace(/\\/g, '/')) } else seen.add(p)
  }
  for (const d of dirs) {
    let real = d
    try { real = realpathSync(d) } catch { /* 解析不出就只走字面路径 */ }
    for (const base of real === d ? [d] : [d, real]) {
      for (const a of ancestors(base)) for (const pat of CLAUDE_MD_PATTERNS) add(join(a, pat))
    }
  }
  return [...seen]
}

// 透传值类型校验：CLI 遇到 flag 文件里任一键类型不对会整份跳过（实测），所以不合就整组不写
function validPassValue(key: string, v: unknown): boolean {
  if (key === 'effortLevel' || key === 'model') return typeof v === 'string' && v.length > 0
  if (key === 'modelSettings') return isPlainObject(v)
  return typeof v === 'boolean'                               // alwaysThinkingEnabled、switchModelsOnFlag
}

function hasEnvModel(d: Record<string, unknown>): boolean {
  return isPlainObject(d.env) && typeof d.env.ANTHROPIC_MODEL === 'string' && d.env.ANTHROPIC_MODEL.length > 0
}

function isDirectory(p: string): boolean {
  try { return statSync(p).isDirectory() } catch { return false }
}

// SKILL.md 开头 frontmatter（首行 --- 到下一行 ---）里 name 的值；容忍 BOM、CRLF、值两端的引号与空白。
// 读不出、没有结束线、没有 name 或值为空都返回 null（调用方照样写目录名）。
// 只读开头 FRONTMATTER_READ_MAX 字节：每次拉起都同步读，SKILL.md 是指向大文件的链接也不整份读进来；结束线不在这一段里按没有算。
const FRONTMATTER_READ_MAX = 8 * 1024
function frontmatterName(file: string): string | null {
  let text: string
  try {
    const fd = openSync(file, 'r')
    try {
      const buf = Buffer.alloc(FRONTMATTER_READ_MAX)
      let n = 0
      while (n < buf.length) {
        const got = readSync(fd, buf, n, buf.length - n, n)
        if (got <= 0) break
        n += got
      }
      text = buf.subarray(0, n).toString('utf8')
    } finally { closeSync(fd) }
  } catch { return null }
  const lines = text.replace(/^﻿/, '').split('\n').map(l => l.replace(/\r$/, ''))
  if (lines[0]?.trimEnd() !== '---') return null
  const end = lines.findIndex((l, i) => i > 0 && l.trimEnd() === '---')
  if (end < 0) return null
  for (const line of lines.slice(1, end)) {
    const m = /^name[ \t]*:(.*)$/.exec(line)
    if (!m) continue
    let v = m[1].trim()
    if (v.length >= 2 && (v[0] === '"' || v[0] === "'") && v[v.length - 1] === v[0]) v = v.slice(1, -1).trim()
    return v.length > 0 ? v : null
  }
  return null
}

const COMMAND_SCAN_MAX_DEPTH = 5        // 命令目录本身算第 1 层，最多读到第 5 层目录
const COMMAND_SCAN_MAX_ENTRIES = 2000   // 一次收集访问的目录条目合计上限（文件与子目录都算）

// 命令目录递归收 .md 文件名（扩展名不分大小写）；跟随链接，按真实路径去重防环；某一层读不出只跳过那一层。
// 准备步骤在每次拉起时同步执行，所以深度与条目数都设上限：碰到指向大目录树的链接也不卡住，超了就停，已收到的照用。
function commandNames(root: string): Set<string> {
  const names = new Set<string>()
  const seen = new Set<string>()
  let budget = COMMAND_SCAN_MAX_ENTRIES
  const walk = (dir: string, depth: number): void => {
    if (budget <= 0) return
    let real: string
    try { real = realpathSync(dir) } catch { return }
    if (seen.has(real)) return
    seen.add(real)
    let handle: Dir
    try { handle = opendirSync(dir) } catch { return }
    try {
      // 逐条读，不一次列出整个目录；Bun 的打开错误要到第一次读才报，同样落进下面的 catch
      for (let e = handle.readSync(); e !== null && budget > 0; e = handle.readSync()) {
        budget--
        const p = join(dir, e.name)
        if (isDirectory(p)) { if (depth < COMMAND_SCAN_MAX_DEPTH) walk(p, depth + 1) }
        else if (e.name.length > 3 && e.name.slice(-3).toLowerCase() === '.md' && isRegularFile(p)) names.add(e.name.slice(0, -3))
      }
    } catch { /* 读到一半出错：这一层到此为止 */ } finally {
      try { handle.closeSync() } catch { /* 打开就失败的句柄关不了，不管 */ }
    }
  }
  try { walk(root, 1) } catch { /* 意外异常：已收到的照用 */ }
  return names
}

// 用户级个人技能（<home>/.claude/skills/<目录>/SKILL.md，目录名 + frontmatter name）与个人命令（<home>/.claude/commands/**.md）的名字。
// 两个目录各读各的；任何读错都只是少几个名字，不抛错、不记 note。不收：生图引擎的目录名与它自己 SKILL.md 的 frontmatter name
// （CLI 按哪个名字认它都不能写进 skillOverrides，否则引擎跟着关掉）；bot 自己 .claude/skills/ 下同名子目录的名字；
// bot 自己 .claude/commands/ 下的命令名（同样递归、同样上限），免得把 bot 自己的命令一起关掉。
function personalOverrideNames(home: string, botDir: string, engineDir: string): string[] {
  const names = new Set<string>()
  const skillsDir = join(home, '.claude', 'skills')
  try {
    for (const e of readdirSync(skillsDir)) {
      const skillFile = join(skillsDir, e, 'SKILL.md')
      if (!isRegularFile(skillFile)) continue                 // stat 跟随链接：技能目录与 SKILL.md 常是链接，与 CLI 加载口径一致
      names.add(e)
      const fmName = frontmatterName(skillFile)
      if (fmName !== null) names.add(fmName)
    }
  } catch { /* 读不出就不收个人技能 */ }
  for (const n of commandNames(join(home, '.claude', 'commands'))) names.add(n)
  const botOwn = commandNames(join(botDir, '.claude', 'commands'))
  const botSkillsDir = join(botDir, '.claude', 'skills')
  try { for (const e of readdirSync(botSkillsDir)) if (isDirectory(join(botSkillsDir, e))) botOwn.add(e) } catch { /* 没有就不排除 */ }
  const engineNames = new Set([SHARED_ENGINE])
  const engineSkill = join(engineDir, 'SKILL.md')
  const engineFm = isRegularFile(engineSkill) ? frontmatterName(engineSkill) : null   // 不是普通文件（如管道）不去打开
  if (engineFm !== null) engineNames.add(engineFm)
  return [...names].filter(n => !engineNames.has(n) && !botOwn.has(n))
}

export function prepareWorkerIsolation(opts: { botDir: string; home: string; user: SettingsLayer; engineDir: string;
                                               platform?: NodeJS.Platform }): PrepareOk | PrepareDegraded {
  const notes: string[] = []
  const addNote = (n: string) => { if (!notes.includes(n)) notes.push(n) }
  const degraded = (reason: DegradeReason): PrepareDegraded => ({ status: 'degraded', reason, notes: [...notes] })
  try {
    const { botDir, home, engineDir } = isPlainObject(opts) ? opts : ({} as Record<string, unknown>)
    if (!isAbsPath(botDir) || !isAbsPath(home) || !isAbsPath(engineDir)) return degraded('prepare_crashed')
    const win = (opts.platform ?? process.platform) === 'win32'
    const user = asLayer(opts.user)

    // 1. 只读：bot 两份设置与用户级一份（用户级由调用方读好传入，透传与 env 补回同源）
    const main = readSettingsFile(join(botDir, '.claude', 'settings.json'))
    const local = readSettingsFile(join(botDir, '.claude', 'settings.local.json'))
    if (main.unreadable || local.unreadable) addNote('bot_settings_unreadable')
    if (user.unreadable) addNote('user_settings_unreadable')

    // 2. 用户级有凭证辅助键而 bot 没设：去掉 user 源会登录不上，不写盘直接降级
    if (hasUserAuthHelper(user, { main, local })) return degraded('user_auth_helper')

    // 3–4. 生图引擎：没装 → 照常隔离、不建链接；装了 → 共享入口自愈，入口读不到 SKILL.md 就降级
    let sharedDir: string | null = null
    if (!isRegularFile(join(engineDir, 'SKILL.md'))) addNote('engine_missing')
    else {
      sharedDir = ensureSharedEngine(home, engineDir, win, addNote)
      if (sharedDir === null) return degraded('shared_unavailable')
    }

    // 5. flag 对象（键顺序固定）：隔离三键 → 透传组 → deny 并集。
    // skillOverrides = 三个自带项 + 个人技能与个人命令，合在一起按 JS 默认排序写出，同样的输入两次准备逐字相同
    const overrideNames = [...new Set([...DISABLED_BUNDLED, ...personalOverrideNames(home, botDir, engineDir)])].sort()
    const flag: Record<string, unknown> = {
      claudeMdExcludes: claudeMdExcludes([botDir, join(home, '.claude', 'worker-shared')], win),
      disableBundledSkills: true,
      skillOverrides: Object.fromEntries(overrideNames.map(n => [n, 'off'])),
    }
    const botTop = new Set([...Object.keys(main.data), ...Object.keys(local.data)])
    for (const group of PASS_GROUPS) {
      let botSet = group.some(k => botTop.has(k))
      // model 组：任一来源的 ANTHROPIC_MODEL 都已决定模型，flag 层再写 model 只会让结果依赖 CLI 的先后规则
      if (group.includes('model')) botSet = botSet || hasEnvModel(main.data) || hasEnvModel(local.data) || hasEnvModel(user.data)
      if (botSet) continue
      const present = group.filter(k => hasKey(user.data, k))
      if (present.length === 0) continue
      if (!present.every(k => validPassValue(k, user.data[k]))) { addNote('pass_value_invalid'); continue }
      for (const k of present) flag[k] = user.data[k]
    }
    const deny: string[] = []
    const addDeny = (perm: unknown, strict: boolean) => {
      if (perm === undefined) return
      if (!isPlainObject(perm)) { if (strict) addNote('pass_value_invalid'); return }
      if (perm.deny === undefined) return
      if (!Array.isArray(perm.deny)) { if (strict) addNote('pass_value_invalid'); return }
      for (const r of perm.deny) if (typeof r === 'string' && !deny.includes(r)) deny.push(r)
    }
    addDeny(user.data.permissions, true)
    addDeny(main.data.permissions, false)
    addDeny(local.data.permissions, false)
    if (deny.length > 0) flag.permissions = { deny }

    // 6. 原子写：本 pid 临时名以 wx/0600 新建写满后 rename（Windows rename 也会覆盖已有文件）
    const flagFile = join(botDir, FLAG_FILE)
    const tmp = `${flagFile}.tmp-${process.pid}`
    try {
      try { unlinkSync(tmp) } catch { /* 没有本 pid 的残留就算了 */ }
      const fd = openSync(tmp, 'wx', 0o600)
      try { writeFileSync(fd, JSON.stringify(flag, null, 2) + '\n') } finally { closeSync(fd) }
      renameSync(tmp, flagFile)
    } catch {
      try { unlinkSync(tmp) } catch { /* ignore */ }
      return degraded('flag_write_failed')
    }

    // 7. 只返回路径、键名与常量，不返回任何 env 值
    const envKeys = new Set<string>()
    for (const d of [main.data, local.data]) if (isPlainObject(d.env)) for (const k of Object.keys(d.env)) envKeys.add(k)
    return { status: 'ok', notes: [...notes], flagFile, sharedDir, botEnvKeys: [...envKeys] }
  } catch {
    return degraded('prepare_crashed')
  }
}

export function workerSpawnArgs(base: string[], prep: PrepareOk, botDir: string): string[] | null {
  if (!Array.isArray(base) || !isPlainObject(prep) || typeof prep.flagFile !== 'string' || !prep.flagFile) return null
  const i = base.indexOf('--setting-sources')
  if (i < 0 || base[i + 1] !== LEGACY_SETTING_SOURCES) return null
  const j = base.findIndex((a, k) => a === '--add-dir' && base[k + 1] === botDir)
  if (j < 0) return null
  const out = [...base]
  out[i + 1] = ISOLATED_SETTING_SOURCES
  if (typeof prep.sharedDir === 'string' && prep.sharedDir) out.splice(j + 2, 0, '--add-dir', prep.sharedDir)
  out.push('--settings', prep.flagFile)
  return out
}

export function isolationInitCheck(init: unknown, expectEngine: boolean): 'flag_ignored' | 'engine_not_loaded' | null {
  if (!isPlainObject(init) || !Array.isArray(init.skills)) return null
  const names = new Set<string>()
  for (const s of init.skills) {
    if (typeof s === 'string') names.add(s)
    else if (isPlainObject(s) && typeof s.name === 'string') names.add(s.name)
  }
  if (DISABLED_BUNDLED.some(n => names.has(n))) return 'flag_ignored'
  if (expectEngine && !names.has(SHARED_ENGINE)) return 'engine_not_loaded'
  return null
}

export function isolationLogLine(x: LogInput): string {
  if (x?.kind === 'off') return `WORKER-ISOLATION mode=off src=${x.src === 'global' ? 'global' : 'switch'}`
  if (x?.kind === 'init_fail') {
    return `WORKER-ISOLATION init_check=fail reason=${INIT_FAIL_REASONS.includes(x.reason) ? x.reason : 'spawn_failed'}`
  }
  const info = x?.kind === 'prep' && isPlainObject(x.info) ? x.info : { status: 'degraded' as const, notes: [] }
  const notes = (Array.isArray(info.notes) ? info.notes : []).filter(n => NOTE_KEYS.includes(n))
  const notesText = notes.length > 0 ? notes.join(',') : '-'
  if (info.status === 'ok') return `WORKER-ISOLATION mode=on status=ok notes=${notesText}`
  const reason = typeof info.reason === 'string' && DEGRADE_REASONS.includes(info.reason) ? info.reason : 'prepare_crashed'
  return `WORKER-ISOLATION mode=on status=degraded reason=${reason} notes=${notesText}`
}

export function summarySpawnPlan(baseEnv: Record<string, string>, user: SettingsLayer, switchOff: boolean):
  { args: string[]; env: Record<string, string>; isolated: boolean } {
  const u = asLayer(user)
  if (switchOff || hasUserAuthHelper(u)) return { args: ['-p', ...LEGACY_SUMMARY_ARGS], env: { ...baseEnv }, isolated: false }
  if (u.unreadable) return { args: ['-p', ...HELPER_ISOLATION_ARGS], env: { ...baseEnv }, isolated: true }
  const env = overlayEnv(baseEnv, globalSettingsEnv(u))
  // 叠加后仍没有非空 ANTHROPIC_MODEL 才用全局顶层 model 补（去掉 user 源后顶层 model 不会再被 CLI 读到）
  const model = u.data.model
  if (!(typeof env.ANTHROPIC_MODEL === 'string' && env.ANTHROPIC_MODEL.length > 0) && typeof model === 'string' && model.length > 0) {
    env.ANTHROPIC_MODEL = model
  }
  return { args: ['-p', ...HELPER_ISOLATION_ARGS], env, isolated: true }
}
