// pub-parity · dispatcher/worker_isolation.ts 公开函数（行为清单 1-3,5-7,9-16 的函数级验证）
// 只依据 pub-parity-接口.md「④ 接口变化与错误契约」。零副作用：HOME 指向临时目录（import 前设好），令牌为假值，不联网。
// 该模块实现前不存在：此文件整体红（模块不存在）是预期的。
import { test, expect, afterAll } from 'bun:test'
import { chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'

const BASE = mkdtempSync(join(tmpdir(), 'pp-iso-'))
process.env.HOME = join(BASE, 'home')
mkdirSync(process.env.HOME, { recursive: true })
afterAll(() => { try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } })

const wi: any = await import('../../../dispatcher/worker_isolation')
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0
const FAKE = 'FAKE-TOKEN-do-not-leak-0123456789'

let seq = 0
function sandbox(opts: { engine?: boolean } = {}) {
  const root = join(BASE, `t${++seq}`)
  const home = join(root, 'home'), botDir = join(root, 'channels', 'bot1'), engineDir = join(root, 'engine', 'novelai-skill')
  mkdirSync(join(botDir, '.claude'), { recursive: true }); mkdirSync(home, { recursive: true })
  if (opts.engine !== false) { mkdirSync(engineDir, { recursive: true }); writeFileSync(join(engineDir, 'SKILL.md'), '# engine\n') }
  return { root, home, botDir, engineDir, link: join(home, '.claude', 'worker-shared', '.claude', 'skills', 'novelai-skill') }
}
const layer = (data: Record<string, unknown> = {}, unreadable = false) => ({ data, unreadable })
const writeBot = (sb: any, main?: unknown, local?: unknown) => {
  if (main !== undefined) writeFileSync(join(sb.botDir, '.claude', 'settings.json'), JSON.stringify(main))
  if (local !== undefined) writeFileSync(join(sb.botDir, '.claude', 'settings.local.json'), JSON.stringify(local))
}
const prep = (sb: any, user: any = layer(), extra: Record<string, unknown> = {}) =>
  wi.prepareWorkerIsolation({ botDir: sb.botDir, home: sb.home, user, engineDir: sb.engineDir, ...extra })
const flagOf = (r: any) => JSON.parse(readFileSync(r.flagFile, 'utf8'))

// ───────── 常量 ─────────
test('常量_与接口文件逐字一致', () => {
  expect(wi.LEGACY_SETTING_SOURCES).toBe('user,project,local')
  expect(wi.ISOLATED_SETTING_SOURCES).toBe('project,local')
  expect(wi.HELPER_ISOLATION_ARGS).toEqual(['--setting-sources', 'project,local', '--strict-mcp-config', '--no-session-persistence'])
  expect(wi.LEGACY_SUMMARY_ARGS).toEqual(['--strict-mcp-config', '--no-session-persistence'])
  expect([wi.SWITCH_OFF_FILE, wi.FLAG_FILE, wi.SHARED_ENGINE]).toEqual(['.worker-isolation-off', '.worker-settings.json', 'novelai-skill'])
  expect(wi.DISABLED_BUNDLED).toEqual(['design', 'doctor', 'plugin-authoring'])
  expect(wi.AUTH_HELPER_KEYS).toEqual(['apiKeyHelper', 'awsAuthRefresh', 'awsCredentialExport', 'gcpAuthRefresh', 'proxyAuthHelper'])
  expect(wi.PROTECTED_ENV_PREFIXES).toEqual(['TELEGRAM_', 'CLAUDEBOTLIFE_', 'CLAUDEBOT_'])
  expect(wi.PROTECTED_ENV_KEYS).toEqual(['PATH', 'HOME', 'CHANNEL_DIR', 'PEERS_JSON'])
})

// ───────── readSettingsFile ─────────
test('readSettingsFile_合法对象与不存在', () => {
  const sb = sandbox(); const ok = join(sb.root, 'a.json'); writeFileSync(ok, '{"a":1}')
  expect(wi.readSettingsFile(ok)).toEqual({ data: { a: 1 }, unreadable: false })
  expect(wi.readSettingsFile(join(sb.root, 'nope.json'))).toEqual({ data: {}, unreadable: false })
  expect(wi.readSettingsFile(join(ok, 'x.json'))).toEqual({ data: {}, unreadable: false })   // ENOTDIR 也算不存在
})
for (const [label, content] of [['坏JSON', '{bad'], ['顶层数组', '[1]'], ['顶层null', 'null'], ['顶层字符串', '"x"']] as const) {
  test(`readSettingsFile_${label}_unreadable为真`, () => {
    const p = join(sandbox().root, 'b.json'); writeFileSync(p, content)
    expect(wi.readSettingsFile(p)).toEqual({ data: {}, unreadable: true })
  })
}
test('readSettingsFile_路径是目录_unreadable为真', () => {
  expect(wi.readSettingsFile(sandbox().root)).toEqual({ data: {}, unreadable: true })
})
test.skipIf(isRoot)('readSettingsFile_无读权限EACCES_unreadable为真', () => {
  const p = join(sandbox().root, 'c.json'); writeFileSync(p, '{}'); chmodSync(p, 0)
  try { expect(wi.readSettingsFile(p)).toEqual({ data: {}, unreadable: true }) } finally { chmodSync(p, 0o600) }
})

// ───────── globalSettingsEnv / overlayEnv / hasUserAuthHelper ─────────
test('globalSettingsEnv_只留字符串值_env类型不对或unreadable返回空对象', () => {
  expect(wi.globalSettingsEnv(layer({ env: { A: '1', B: 2, C: null, D: 'x', E: '' } }))).toEqual({ A: '1', D: 'x', E: '' })
  expect(wi.globalSettingsEnv(layer({ env: ['A'] }))).toEqual({})
  expect(wi.globalSettingsEnv(layer({ env: 'A=1' }))).toEqual({})
  expect(wi.globalSettingsEnv(layer({ env: { A: '1' } }, true))).toEqual({})
})
test('overlayEnv_受保护前缀与固定键不被覆盖_大小写不敏感_其余含代理变量照叠加', () => {
  const target = { PATH: '/bin', HOME: '/h', CHANNEL_DIR: '/c', TELEGRAM_X: 't' }
  const extra = { TELEGRAM_BOT_TOKEN: FAKE, telegram_chat: 'y', CLAUDEBOT_A: '1', claudebotlife_b: '2', PATH: '/evil', home: '/evil',
    Channel_Dir: '/evil', peers_json: '{}', HTTPS_PROXY: 'http://p', EXTRA: 'v' }
  const out = wi.overlayEnv(target, extra)
  expect(out).toEqual({ ...target, HTTPS_PROXY: 'http://p', EXTRA: 'v' })
})
test('overlayEnv_skipKeys不被覆盖_大小写不敏感', () => {
  const out = wi.overlayEnv({ ANTHROPIC_BASE_URL: 'bot-value' }, { ANTHROPIC_BASE_URL: 'global', OTHER: 'o' }, ['anthropic_base_url'])
  expect(out).toEqual({ ANTHROPIC_BASE_URL: 'bot-value', OTHER: 'o' })
})
test('overlayEnv_不改入参_返回新对象', () => {
  const target = { A: '1' }, extra = { B: '2' }
  const out = wi.overlayEnv(target, extra, ['X'])
  expect(out).not.toBe(target); expect(target).toEqual({ A: '1' }); expect(extra).toEqual({ B: '2' })
})
test('hasUserAuthHelper_五个凭证键各自为真_空值与unreadable为假_bot已设同名键不算', () => {
  for (const k of wi.AUTH_HELPER_KEYS) expect(wi.hasUserAuthHelper(layer({ [k]: '/bin/x' }))).toBe(true)
  for (const v of [null, '', false]) expect(wi.hasUserAuthHelper(layer({ apiKeyHelper: v }))).toBe(false)
  expect(wi.hasUserAuthHelper(layer({ apiKeyHelper: '/bin/x' }, true))).toBe(false)
  expect(wi.hasUserAuthHelper(layer({ forceLoginMethod: 'claudeai' }))).toBe(false)
  expect(wi.hasUserAuthHelper(layer({ apiKeyHelper: '/bin/x' }), { main: layer({ apiKeyHelper: 'y' }), local: layer() })).toBe(false)
  expect(wi.hasUserAuthHelper(layer({ apiKeyHelper: '/bin/x' }), { main: layer(), local: layer() })).toBe(true)
})

// ───────── isolationSwitchOff（行为清单 9 的判定部分）─────────
const MARK = '.worker-isolation-off'
test('isolationSwitchOff_bot目录有标记_switch', () => {
  const sb = sandbox(); writeFileSync(join(sb.botDir, MARK), '')
  expect(wi.isolationSwitchOff(sb.botDir)).toBe('switch')
})
test('isolationSwitchOff_只有频道根有标记_global', () => {
  const sb = sandbox(); writeFileSync(join(dirname(sb.botDir), MARK), '')
  expect(wi.isolationSwitchOff(sb.botDir)).toBe('global')
})
test('isolationSwitchOff_两处都有_switch优先', () => {
  const sb = sandbox(); writeFileSync(join(sb.botDir, MARK), ''); writeFileSync(join(dirname(sb.botDir), MARK), '')
  expect(wi.isolationSwitchOff(sb.botDir)).toBe('switch')
})
test('isolationSwitchOff_都没有_null', () => {
  expect(wi.isolationSwitchOff(sandbox().botDir)).toBeNull()
})
test('isolationSwitchOff_悬空链接与目录也算存在', () => {
  const a = sandbox(); symlinkSync(join(a.root, 'no-such-target'), join(a.botDir, MARK))
  expect(wi.isolationSwitchOff(a.botDir)).toBe('switch')
  const b = sandbox(); mkdirSync(join(dirname(b.botDir), MARK))
  expect(wi.isolationSwitchOff(b.botDir)).toBe('global')
})
test('isolationSwitchOff_非绝对路径与根目录_null', () => {
  expect(wi.isolationSwitchOff('relative/bot')).toBeNull()
  expect(wi.isolationSwitchOff('')).toBeNull()
  expect(wi.isolationSwitchOff('/')).toBeNull()
})

// ───────── prepareWorkerIsolation：正常路径（行为清单 1、2、3）─────────
test('prepare_生图技能已装_共享链接指向引擎_flag写在bot目录_无engine_missing', () => {
  const sb = sandbox(); const r = prep(sb)
  expect(r.status).toBe('ok')
  expect(r.flagFile).toBe(join(sb.botDir, '.worker-settings.json'))
  expect(r.sharedDir).toBe(join(sb.home, '.claude', 'worker-shared'))
  expect(lstatSync(sb.link).isSymbolicLink()).toBe(true)
  expect(readlinkSync(sb.link)).toBe(sb.engineDir)
  expect(statSync(join(sb.link, 'SKILL.md')).isFile()).toBe(true)
  expect(r.notes).not.toContain('engine_missing')
})
test('prepare_flag文件内容_关内置技能_排除全局CLAUDE_md_无env钩子插件_格式与权限固定', () => {
  const sb = sandbox(); const r = prep(sb)
  const text = readFileSync(r.flagFile, 'utf8'); const f = JSON.parse(text)
  expect(f.disableBundledSkills).toBe(true)
  expect(f.skillOverrides).toEqual({ design: 'off', doctor: 'off', 'plugin-authoring': 'off' })
  expect(Array.isArray(f.claudeMdExcludes) && f.claudeMdExcludes.length > 0).toBe(true)
  for (const k of ['env', 'hooks', 'enabledPlugins', 'permissions']) expect(k in f).toBe(false)
  expect(Object.keys(f).slice(0, 3)).toEqual(['claudeMdExcludes', 'disableBundledSkills', 'skillOverrides'])
  expect(text).toBe(JSON.stringify(f, null, 2) + '\n')
  if (process.platform !== 'win32') expect(statSync(r.flagFile).mode & 0o777).toBe(0o600)
})
test('prepare_再准备一次_链接readlink不变_flag逐字相同_无临时文件残留', () => {
  const sb = sandbox(); const r1 = prep(sb); const b1 = readFileSync(r1.flagFile); const l1 = readlinkSync(sb.link)
  const r2 = prep(sb)
  expect(r2.status).toBe('ok'); expect(readlinkSync(sb.link)).toBe(l1); expect(readFileSync(r2.flagFile).equals(b1)).toBe(true)
  expect(readdirSync(sb.botDir).filter(n => n.includes('.tmp-'))).toEqual([])
  expect(readdirSync(dirname(sb.link)).filter(n => n.includes('.tmp-'))).toEqual([])
})
test('prepare_没装生图技能_照常隔离_notes含engine_missing_sharedDir为null_不建链接', () => {
  const sb = sandbox({ engine: false }); const r = prep(sb)
  expect(r.status).toBe('ok'); expect(r.notes).toContain('engine_missing'); expect(r.sharedDir).toBeNull()
  expect(existsSync(join(sb.home, '.claude', 'worker-shared', '.claude', 'skills', 'novelai-skill'))).toBe(false)
  expect(existsSync(r.flagFile)).toBe(true)
})
test('prepare_botEnvKeys只含两份env的键名并集_返回值不含任何env值', () => {
  const sb = sandbox(); writeBot(sb, { env: { A_KEY: 'secretvalue1' } }, { env: { B_KEY: 'secretvalue2', A_KEY: 'dup' } })
  const r = prep(sb)
  expect([...r.botEnvKeys].sort()).toEqual(['A_KEY', 'B_KEY']); expect(JSON.stringify(r)).not.toContain('secretvalue')
})

// ───────── 透传（行为清单 5、6、7）─────────
test('prepare_bot绑了供应商_全局有model_flag没有model也没有env', () => {
  const sb = sandbox(); writeBot(sb, { env: { ANTHROPIC_BASE_URL: 'http://prov.invalid', ANTHROPIC_AUTH_TOKEN: FAKE, ANTHROPIC_MODEL: 'prov-model' } })
  const f = flagOf(prep(sb, layer({ model: 'global-model' })))
  expect('model' in f).toBe(false); expect('env' in f).toBe(false); expect(JSON.stringify(f)).not.toContain(FAKE)
})
test('prepare_bot什么都没设_全局env有ANTHROPIC_MODEL且顶层有model_flag也没有model', () => {
  const sb = sandbox()
  expect('model' in flagOf(prep(sb, layer({ model: 'global-model', env: { ANTHROPIC_MODEL: 'env-model' } })))).toBe(false)
})
test('prepare_只有全局顶层model_flag有它', () => {
  expect(flagOf(prep(sandbox(), layer({ model: 'global-model' }))).model).toBe('global-model')
})
test('prepare_全局effortLevel_bot两份都没设effortLevel与modelSettings_flag有effortLevel', () => {
  expect(flagOf(prep(sandbox(), layer({ effortLevel: 'high' }))).effortLevel).toBe('high')
})
for (const [label, main, local] of [['settings.json设了effortLevel', { effortLevel: 'low' }, undefined], ['settings.local.json设了modelSettings', undefined, { modelSettings: {} }]] as const) {
  test(`prepare_bot${label}_flag里effortLevel与modelSettings两键都没有`, () => {
    const sb = sandbox(); writeBot(sb, main, local)
    const f = flagOf(prep(sb, layer({ effortLevel: 'high', modelSettings: { x: 1 } })))
    expect('effortLevel' in f).toBe(false); expect('modelSettings' in f).toBe(false)
  })
}
test('prepare_全局alwaysThinkingEnabled是字符串_该组不写_notes含pass_value_invalid', () => {
  const r = prep(sandbox(), layer({ alwaysThinkingEnabled: 'yes' }))
  expect(r.status).toBe('ok'); expect('alwaysThinkingEnabled' in flagOf(r)).toBe(false); expect(r.notes).toContain('pass_value_invalid')
})
test('prepare_全局有tui_flag里没有tui', () => {
  expect('tui' in flagOf(prep(sandbox(), layer({ tui: 'fullscreen', effortLevel: 'high' })))).toBe(false)
})
test('prepare_permissions_deny取全局与bot的并集_没有allow_ask_defaultMode', () => {
  const sb = sandbox(); writeBot(sb, undefined, { permissions: { deny: ['Read(./b)', 'Read(./a)'] } })
  const f = flagOf(prep(sb, layer({ permissions: { deny: ['Bash(rm:*)', 'Read(./a)'], allow: ['Bash(ls:*)'], ask: ['Bash(git push:*)'], defaultMode: 'acceptEdits' } })))
  expect(Object.keys(f.permissions)).toEqual(['deny'])
  expect([...f.permissions.deny].sort()).toEqual(['Bash(rm:*)', 'Read(./a)', 'Read(./b)'])
})
test('prepare_两边都没有deny_flag没有permissions', () => {
  expect('permissions' in flagOf(prep(sandbox(), layer({ permissions: { allow: ['Read'] } })))).toBe(false)
})

// ───────── 用户级设置异常 / 凭证辅助键（行为清单 10、11）─────────
test('prepare_用户级settings坏JSON_照常隔离_notes含user_settings_unreadable_flag无透传键', () => {
  const sb = sandbox(); const p = join(sb.root, 'user-settings.json'); writeFileSync(p, '{"model": "x", ')
  const r = prep(sb, wi.readSettingsFile(p))
  expect(r.status).toBe('ok'); expect(r.notes).toContain('user_settings_unreadable')
  const f = flagOf(r)
  for (const k of ['model', 'effortLevel', 'alwaysThinkingEnabled', 'permissions', 'env']) expect(k in f).toBe(false)
})
for (const key of ['apiKeyHelper', 'awsAuthRefresh', 'awsCredentialExport', 'gcpAuthRefresh', 'proxyAuthHelper']) {
  test(`prepare_用户级有${key}_bot没设_降级user_auth_helper_不写任何盘`, () => {
    const sb = sandbox(); const r = prep(sb, layer({ [key]: '/bin/helper' }))
    expect(r.status).toBe('degraded'); expect(r.reason).toBe('user_auth_helper')
    expect(existsSync(join(sb.botDir, '.worker-settings.json'))).toBe(false)
    expect(existsSync(join(sb.home, '.claude', 'worker-shared'))).toBe(false)
  })
}
test('prepare_只有forceLoginMethod_照常隔离', () => {
  expect(prep(sandbox(), layer({ forceLoginMethod: 'claudeai' })).status).toBe('ok')
})
test('prepare_bot的settings已设同名凭证键_不降级', () => {
  const sb = sandbox(); writeBot(sb, { apiKeyHelper: '/bot/helper' })
  expect(prep(sb, layer({ apiKeyHelper: '/bin/helper' })).status).toBe('ok')
})

// ───────── Windows 分支（行为清单 12，注入 platform）与共享目录失败 ─────────
test('prepare_win32_共享入口指向别处的链接_删联接本身后重建_notes含shared_wrong_link', () => {
  const sb = sandbox(); const other = join(sb.root, 'other'); mkdirSync(other); mkdirSync(dirname(sb.link), { recursive: true }); symlinkSync(other, sb.link)
  const r = prep(sb, layer(), { platform: 'win32' })
  expect(r.status).toBe('ok'); expect(r.notes).toContain('shared_wrong_link'); expect(readlinkSync(sb.link)).toBe(sb.engineDir)
  expect(existsSync(join(other))).toBe(true)                                 // 不碰链接目标
})
for (const platform of [undefined, 'win32'] as const) {
  test(`prepare_${platform ?? '本机平台'}_共享入口是不含SKILL_md的真实目录_降级shared_unavailable`, () => {
    const sb = sandbox(); mkdirSync(sb.link, { recursive: true })
    const r = prep(sb, layer(), platform ? { platform } : {})
    expect(r.status).toBe('degraded'); expect(r.reason).toBe('shared_unavailable'); expect(r.notes).toContain('shared_conflict')
    expect(existsSync(sb.link)).toBe(true)                                   // 不动真实目录
  })
}
test('prepare_win32_排除项里斜杠与反斜杠两种分隔符都在', () => {
  const ex: string[] = flagOf(prep(sandbox(), layer(), { platform: 'win32' })).claudeMdExcludes
  expect(ex.some(e => e.includes('\\'))).toBe(true); expect(ex.some(e => e.includes('/'))).toBe(true)
})

// ───────── 降级：路径非法 / flag 写不成 ─────────
test('prepare_路径参数不是绝对路径_降级prepare_crashed_不写任何文件', () => {
  const sb = sandbox(); const r = wi.prepareWorkerIsolation({ botDir: 'relative/bot', home: sb.home, user: layer(), engineDir: sb.engineDir })
  expect(r.status).toBe('degraded'); expect(r.reason).toBe('prepare_crashed')
  expect(existsSync(join(sb.home, '.claude'))).toBe(false)
})
test.skipIf(isRoot)('prepare_bot目录只读_flag写不成_降级flag_write_failed_无临时文件残留', () => {
  const sb = sandbox(); chmodSync(sb.botDir, 0o555)
  try {
    const r = prep(sb)
    expect(r.status).toBe('degraded'); expect(r.reason).toBe('flag_write_failed')
    expect(readdirSync(sb.botDir).filter(n => n.startsWith('.worker-settings.json'))).toEqual([])
  } finally { chmodSync(sb.botDir, 0o755) }
})

// ───────── workerSpawnArgs（行为清单 1 的参数形态、13）─────────
const okPrep = (flag: string, shared: string | null) => ({ status: 'ok', notes: [], flagFile: flag, sharedDir: shared, botEnvKeys: [] })
const baseArgs = (bot: string) => ['--output-format', 'stream-json', '--setting-sources', 'user,project,local', '--add-dir', bot, '--model', 'm', '--verbose']
test('workerSpawnArgs_换成project_local_共享目录紧跟bot目录_末尾加settings_其余顺序不变_不改入参', () => {
  const base = baseArgs('/b'); const snap = [...base]
  expect(wi.workerSpawnArgs(base, okPrep('/b/.worker-settings.json', '/h/.claude/worker-shared'), '/b')).toEqual(
    ['--output-format', 'stream-json', '--setting-sources', 'project,local', '--add-dir', '/b', '--add-dir', '/h/.claude/worker-shared',
      '--model', 'm', '--verbose', '--settings', '/b/.worker-settings.json'])
  expect(base).toEqual(snap)
})
test('workerSpawnArgs_没有共享目录_不加第二个add_dir', () => {
  const out = wi.workerSpawnArgs(baseArgs('/b'), okPrep('/b/.worker-settings.json', null), '/b')
  expect(out.filter((a: string) => a === '--add-dir').length).toBe(1); expect(out.slice(-2)).toEqual(['--settings', '/b/.worker-settings.json'])
})
for (const [label, base] of [
  ['没有--setting-sources', ['--add-dir', '/b', '--verbose']],
  ['--setting-sources后不是user_project_local', ['--setting-sources', 'project,local', '--add-dir', '/b']],
  ['没有--add-dir botDir', ['--setting-sources', 'user,project,local', '--add-dir', '/elsewhere']],
] as const) {
  test(`workerSpawnArgs_基础参数${label}_返回null`, () => {
    expect(wi.workerSpawnArgs([...base], okPrep('/b/.worker-settings.json', null), '/b')).toBeNull()
  })
}

// ───────── isolationInitCheck（行为清单 14 的判定部分）─────────
const INIT_CASES: [string, unknown, boolean, string | null][] = [
  ['字符串技能含doctor', { skills: ['doctor'] }, false, 'flag_ignored'],
  ['对象技能含design', { skills: [{ name: 'design' }] }, true, 'flag_ignored'],
  ['含plugin-authoring', { skills: ['plugin-authoring', 'novelai-skill'] }, true, 'flag_ignored'],
  ['期望生图但没有', { skills: ['other'] }, true, 'engine_not_loaded'],
  ['期望生图且在', { skills: ['other', 'novelai-skill'] }, true, null],
  ['不期望生图', { skills: ['other'] }, false, null],
  ['init是null', null, true, null], ['init是字符串', 'x', true, null], ['skills不是数组', { skills: 'doctor' }, true, null], ['没有skills', {}, true, null],
]
for (const [label, init, expectEngine, want] of INIT_CASES) {
  test(`isolationInitCheck_${label}_${want}`, () => { expect(wi.isolationInitCheck(init, expectEngine)).toBe(want) })
}

// ───────── isolationLogLine（日志行格式）─────────
const LOG_RE = /^WORKER-ISOLATION (mode=(off src=(switch|global)|on status=(ok|degraded reason=[a-z_]+) notes=[a-z_,-]+)|init_check=fail reason=[a-z_]+)$/
const LOG_CASES: [unknown, string][] = [
  [{ kind: 'off', src: 'switch' }, 'WORKER-ISOLATION mode=off src=switch'],
  [{ kind: 'off', src: 'global' }, 'WORKER-ISOLATION mode=off src=global'],
  [{ kind: 'prep', info: { status: 'ok', notes: [] } }, 'WORKER-ISOLATION mode=on status=ok notes=-'],
  [{ kind: 'prep', info: { status: 'ok', notes: ['engine_missing', 'shared_wrong_link'] } }, 'WORKER-ISOLATION mode=on status=ok notes=engine_missing,shared_wrong_link'],
  [{ kind: 'prep', info: { status: 'degraded', reason: 'user_auth_helper', notes: [] } }, 'WORKER-ISOLATION mode=on status=degraded reason=user_auth_helper notes=-'],
  [{ kind: 'init_fail', reason: 'flag_ignored' }, 'WORKER-ISOLATION init_check=fail reason=flag_ignored'],
]
for (const [input, want] of LOG_CASES) {
  test(`isolationLogLine_${want.replace('WORKER-ISOLATION ', '')}`, () => {
    const line = wi.isolationLogLine(input); expect(line).toBe(want); expect(LOG_RE.test(line)).toBe(true)
  })
}

// ───────── summarySpawnPlan（行为清单 15、16 的参数与环境）─────────
const SUM_ISO = ['-p', '--setting-sources', 'project,local', '--strict-mcp-config', '--no-session-persistence']
const SUM_OLD = ['-p', '--strict-mcp-config', '--no-session-persistence']
test('summarySpawnPlan_隔离_全局env补进环境_叠加后没有非空ANTHROPIC_MODEL才用顶层model补', () => {
  const r = wi.summarySpawnPlan({ PATH: '/bin' }, layer({ env: { HTTPS_PROXY: 'http://p', ANTHROPIC_MODEL: '' }, model: 'glm-x' }), false)
  expect(r.args).toEqual(SUM_ISO); expect(r.isolated).toBe(true)
  expect(r.env).toEqual({ PATH: '/bin', HTTPS_PROXY: 'http://p', ANTHROPIC_MODEL: 'glm-x' })
})
test('summarySpawnPlan_全局env已有ANTHROPIC_MODEL_不用顶层model覆盖', () => {
  const r = wi.summarySpawnPlan({}, layer({ env: { ANTHROPIC_MODEL: 'env-model' }, model: 'top-model' }), false)
  expect(r.env.ANTHROPIC_MODEL).toBe('env-model')
})
test('summarySpawnPlan_关闭标记存在_旧参数_env与改动前相同', () => {
  const base = { PATH: '/bin' }
  const r = wi.summarySpawnPlan(base, layer({ env: { HTTPS_PROXY: 'http://p' }, model: 'm' }), true)
  expect(r.args).toEqual(SUM_OLD); expect(r.isolated).toBe(false); expect(r.env).toEqual(base); expect(r.env).not.toBe(base)
})
test('summarySpawnPlan_用户级有凭证辅助键_旧参数_env与改动前相同', () => {
  const r = wi.summarySpawnPlan({ PATH: '/bin' }, layer({ apiKeyHelper: '/bin/x', env: { HTTPS_PROXY: 'http://p' } }), false)
  expect(r.args).toEqual(SUM_OLD); expect(r.isolated).toBe(false); expect(r.env).toEqual({ PATH: '/bin' })
})
test('summarySpawnPlan_全局设置读不到_仍隔离_不补env也不补model', () => {
  const r = wi.summarySpawnPlan({ PATH: '/bin' }, layer({ env: { HTTPS_PROXY: 'http://p' }, model: 'm' }, true), false)
  expect(r.args).toEqual(SUM_ISO); expect(r.isolated).toBe(true); expect(r.env).toEqual({ PATH: '/bin' })
})
