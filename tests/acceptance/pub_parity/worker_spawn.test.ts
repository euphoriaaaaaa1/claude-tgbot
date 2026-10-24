// pub-parity · WorkerManager 拉起时的可观察行为（行为清单 1,2,3,4,8,9,10,11,13）
// 观察点：经 spawn 捕获的参数与子进程环境、临时 HOME 下的 flag 与共享目录、拉起日志。接缝假设见 wm_harness.ts 文件头。
// 自检降级（行为清单 14）每个场景单独一个文件（worker_selfcheck_*.test.ts），避免内存位在同一进程里互相污染。
import { test, expect, beforeEach } from 'bun:test'
import { chmodSync, readFileSync, readlinkSync } from 'node:fs'
import { FAKE_AUTH, FAKE_BOT_TOKEN, boot } from './wm_harness'

const h = await boot()
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0
beforeEach(() => h.reset())

/** 按接口文件「三种结果」表，从改动前参数推出隔离成功时应有的参数。 */
function expectedIsolated(legacyArgs: string[], withShared = true): string[] {
  const a = [...legacyArgs]
  a[a.indexOf('--setting-sources') + 1] = 'project,local'
  const i = a.findIndex((x, k) => x === '--add-dir' && a[k + 1] === h.botDir)
  if (withShared) a.splice(i + 2, 0, '--add-dir', h.shared)
  a.push('--settings', h.flag)
  return a
}
const flagOf = () => JSON.parse(readFileSync(h.flag, 'utf8'))

// ── 行为清单 13（后半）：WorkerManager 的基础参数形态由测试锁住 ──
test('基础参数形态_开关关着时仍含setting-sources三源与add-dir到bot目录', async () => {
  const c = await h.legacy()
  expect(h.argAfter(c.args, '--setting-sources')).toBe('user,project,local')
  expect(c.args.findIndex((x, k) => x === '--add-dir' && c.args[k + 1] === h.botDir)).toBeGreaterThanOrEqual(0)
})

// ── 行为清单 1 ──
test('默认准备成功_参数按接口改写_其余参数与顺序不变_日志mode_on_status_ok', async () => {
  const legacy = await h.legacy()
  const from = h.logs.length
  const iso = await h.launch(h.newManager())
  expect(iso.args).toEqual(expectedIsolated(legacy.args))
  const lines = h.isoLines(from)
  expect(lines.length).toBe(1)
  expect(lines[0]).toMatch(/^WORKER-ISOLATION mode=on status=ok notes=[a-z_,-]+$/)
})

// ── 行为清单 2 ──
test('再拉起一次_共享链接readlink不变_flag文件逐字相同', async () => {
  await h.launch(h.newManager())
  const link1 = readlinkSync(h.link), flag1 = readFileSync(h.flag)
  expect(link1).toBe(h.engineDir)
  await h.launch(h.newManager())
  expect(readlinkSync(h.link)).toBe(link1)
  expect(readFileSync(h.flag).equals(flag1)).toBe(true)
})

// ── 行为清单 3 ──
test('没装生图技能_照常隔离_没有共享add-dir_不建链接_notes含engine_missing', async () => {
  h.removeEngine()
  const legacy = await h.legacy()
  const from = h.logs.length
  const iso = await h.launch(h.newManager())
  expect(iso.args).toEqual(expectedIsolated(legacy.args, false))
  expect(h.exists(h.link)).toBe(false)
  expect(h.isoLines(from)[0]).toMatch(/^WORKER-ISOLATION mode=on status=ok notes=.*engine_missing/)
})

// ── 行为清单 4 ──
test('全局env叠进子进程环境_受保护键与bot已有键不被覆盖_非字符串值丢弃', async () => {
  h.setUser({ env: { HTTPS_PROXY: 'http://127.0.0.1:1', EXTRA_STR: 'v', NUM: 5, TELEGRAM_BOT_TOKEN: 'GLOBAL-OVERRIDE', CLAUDEBOT_Y: '2',
    CLAUDEBOTLIFE_X: '1', PATH: '/evil', HOME: '/evil', CHANNEL_DIR: '/evil', PEERS_JSON: '{}', ANTHROPIC_BASE_URL: 'http://global.invalid' } })
  h.setBot({ env: { ANTHROPIC_BASE_URL: 'http://bot.invalid' } })
  const legacy = await h.legacy()
  const iso = await h.launch(h.newManager())
  expect(iso.env).toEqual({ ...legacy.env, HTTPS_PROXY: 'http://127.0.0.1:1', EXTRA_STR: 'v' })
  expect(h.logs.join('')).not.toContain('GLOBAL-OVERRIDE')
})

// ── 行为清单 8 ──
test.skipIf(isRoot)('flag写不成_worker仍被拉起_参数环境与改动前相同_日志降级flag_write_failed_不含令牌', async () => {
  const legacy = await h.legacy()
  const from = h.logs.length
  chmodSync(h.botDir, 0o555)
  let c
  try { c = await h.launch(h.newManager()) } finally { chmodSync(h.botDir, 0o755) }
  expect(c.args).toEqual(legacy.args)
  expect(c.env).toEqual(legacy.env)
  const lines = h.isoLines(from)
  expect(lines.length).toBe(1)
  expect(lines[0]).toMatch(/^WORKER-ISOLATION mode=on status=degraded reason=flag_write_failed notes=[a-z_,-]+$/)
  expect(h.logs.join('')).not.toContain(FAKE_BOT_TOKEN)
  expect(h.logs.join('')).not.toContain(FAKE_AUTH)
})

// ── 行为清单 9 ──
for (const [where, src] of [['bot', 'switch'], ['root', 'global']] as const) {
  test(`关闭标记在${where === 'bot' ? 'bot目录' : '频道根'}_参数环境与改动前相同_不建共享目录不写flag_日志src_${src}`, async () => {
    h.setUser({ env: { HTTPS_PROXY: 'http://127.0.0.1:1' } })
    const ref = await h.legacy()
    h.markSwitch(where)
    const from = h.logs.length
    const c = await h.launch(h.newManager())
    expect(c.args).toEqual(ref.args)
    expect(c.env).toEqual(ref.env)
    expect(h.argAfter(c.args, '--setting-sources')).toBe('user,project,local')
    expect('HTTPS_PROXY' in c.env).toBe(false)
    expect(h.exists(h.shared)).toBe(false)
    expect(h.exists(h.flag)).toBe(false)
    expect(h.isoLines(from)).toEqual([`WORKER-ISOLATION mode=off src=${src}`])
  })
}

// ── 行为清单 10 ──
test('用户级settings坏JSON_照常隔离_notes含user_settings_unreadable_flag无透传键', async () => {
  h.setUser('{"env": {', 'settings.json')
  const from = h.logs.length
  const c = await h.launch(h.newManager())
  expect(h.argAfter(c.args, '--setting-sources')).toBe('project,local')
  expect(h.isoLines(from)[0]).toMatch(/^WORKER-ISOLATION mode=on status=ok notes=.*user_settings_unreadable/)
  const f = flagOf()
  for (const k of ['model', 'effortLevel', 'alwaysThinkingEnabled', 'permissions', 'env']) expect(k in f).toBe(false)
})

test('用户级settings_local里的env与model_既不进子进程环境也不进flag', async () => {
  h.setUser({}, 'settings.json')
  h.setUser({ env: { LOCAL_ONLY_VAR: 'x' }, model: 'local-model' }, 'settings.local.json')
  const legacy = await h.legacy()
  const c = await h.launch(h.newManager())
  expect(c.env).toEqual(legacy.env)
  expect('LOCAL_ONLY_VAR' in c.env).toBe(false)
  expect('model' in flagOf()).toBe(false)
})

// ── 行为清单 11 ──
test('用户级有apiKeyHelper_bot没设_降级user_auth_helper_参数环境与改动前相同_不叠env不写flag', async () => {
  h.setUser({ apiKeyHelper: '/bin/helper', env: { HTTPS_PROXY: 'http://127.0.0.1:1' } })
  const legacy = await h.legacy()
  const from = h.logs.length
  const c = await h.launch(h.newManager())
  expect(c.args).toEqual(legacy.args)
  expect(c.env).toEqual(legacy.env)
  expect(h.isoLines(from)[0]).toMatch(/^WORKER-ISOLATION mode=on status=degraded reason=user_auth_helper notes=[a-z_,-]+$/)
  expect(h.exists(h.flag)).toBe(false)
})

test('用户级只有forceLoginMethod_照常隔离', async () => {
  h.setUser({ forceLoginMethod: 'claudeai' })
  const from = h.logs.length
  const c = await h.launch(h.newManager())
  expect(h.argAfter(c.args, '--setting-sources')).toBe('project,local')
  expect(h.isoLines(from)[0]).toMatch(/^WORKER-ISOLATION mode=on status=ok /)
})
