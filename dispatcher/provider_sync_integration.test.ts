// 白盒测试 · provider_watch —— 全程临时目录 + 假凭证 + 注入时钟，绝不碰真实 ~/.claude/settings.json。
import { test, expect, afterEach } from 'bun:test'
import { mkdtempSync, writeFileSync, rmSync, renameSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'
import { startProviderWatch, settingsPath, type ProviderWatchHandle } from './provider_watch'

const FAKE_TOKEN = 'sk-FAKE-int-abc123'
const mkSettings = (over: Record<string, unknown> = {}, top: Record<string, unknown> = {}) =>
  JSON.stringify({
    env: {
      ANTHROPIC_BASE_URL: 'https://api.fake-provider.test/anthropic',
      ANTHROPIC_AUTH_TOKEN: FAKE_TOKEN,
      ANTHROPIC_MODEL: 'fake-model-x1',
      ...over,
    },
    hooks: { PreToolUse: [] },
    ...top,
  })

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

const live: ProviderWatchHandle[] = []
const dirs: string[] = []
afterEach(() => {
  while (live.length) live.pop()!.stop()
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

// 一套隔离夹具：临时目录 + 假 settings.json + 计数器 + 日志收集 + 可拨动的时钟
function fixture(initial: string | null = mkSettings(), debounceMs = 60) {
  const dir = mkdtempSync(join(tmpdir(), 'provider-sync-test-'))
  dirs.push(dir)
  const file = join(dir, 'settings.json')
  if (initial !== null) writeFileSync(file, initial)
  const logs: string[] = []
  let hits = 0
  let clock = 1_000_000
  const w = startProviderWatch({
    path: file,
    debounceMs,
    pollMs: 5_000,                 // 轮询不参与断言，靠 evaluate()/fs.watch 驱动
    now: () => clock,
    log: l => logs.push(l),
    onProviderChanged: () => { hits++ },
  })
  live.push(w)
  return {
    file, logs, w,
    hits: () => hits,
    tick: (ms: number) => { clock += ms },
    write: (text: string) => writeFileSync(file, text),
    // 原子替换（cc-switch/编辑器的写法）：写 tmp 再 rename 盖上去
    atomicWrite: (text: string) => {
      writeFileSync(`${file}.tmp`, text)
      renameSync(`${file}.tmp`, file)
    },
  }
}

test('启动只记基线不触发_内容没变也不触发', () => {
  const f = fixture()
  expect(f.hits()).toBe(0)
  f.w.evaluate()
  expect(f.hits()).toBe(0)
  expect(f.logs).toEqual([])          // 文件没动过 → 一行日志都不刷
})

test('provider字段变化_触发一次重启', () => {
  const f = fixture()
  f.tick(10_000)
  f.write(mkSettings({ ANTHROPIC_BASE_URL: 'https://api.other-fake.test/anthropic' }))
  f.w.evaluate()
  expect(f.hits()).toBe(1)
  expect(f.logs).toContain('refresh=true reason=provider_changed')
})

test('无关字段变化_零动作只记一行跳过', () => {
  const f = fixture()
  f.tick(10_000)
  f.write(mkSettings({}, { hooks: { PreToolUse: [{ matcher: 'Bash' }] }, theme: 'dark' }))
  f.w.evaluate()
  expect(f.hits()).toBe(0)
  expect(f.logs).toEqual(['refresh=false reason=irrelevant_change'])
})

test('文件被删_不触发不崩_删后换新provider回来仍能认出', () => {
  const f = fixture()
  f.tick(10_000)
  rmSync(f.file, { force: true })
  f.w.evaluate()
  expect(f.hits()).toBe(0)
  expect(f.logs).toContain('refresh=false reason=unreadable')
  // 关键：空签名不能把基线冲掉，否则文件带着新 provider 回来会被当成「首次基线」漏掉
  f.tick(10_000)
  f.write(mkSettings({ ANTHROPIC_MODEL: 'fake-model-x9' }))
  f.w.evaluate()
  expect(f.hits()).toBe(1)
})

test('JSON写坏_不触发_修回原provider也不白重启', () => {
  const f = fixture()
  f.tick(10_000)
  f.write('{"env": {oops')
  f.w.evaluate()
  expect(f.hits()).toBe(0)
  f.tick(10_000)
  f.write(mkSettings())              // 内容与基线等价
  f.w.evaluate()
  expect(f.hits()).toBe(0)
})

test('文件一开始不存在_后来出现只当基线不触发', () => {
  const f = fixture(null)
  f.tick(10_000)
  f.write(mkSettings())
  f.w.evaluate()
  expect(f.hits()).toBe(0)
  expect(f.logs).toContain('refresh=false reason=baseline')
  // 基线立住之后再换才算真变
  f.tick(10_000)
  f.write(mkSettings({ ANTHROPIC_MODEL: 'fake-model-x2' }))
  f.w.evaluate()
  expect(f.hits()).toBe(1)
})

test('防抖窗口内的第二次变化_先压掉_窗口过了再补上', () => {
  const f = fixture()
  f.tick(10_000)
  f.write(mkSettings({ ANTHROPIC_MODEL: 'fake-model-x2' }))
  f.w.evaluate()
  expect(f.hits()).toBe(1)
  // 时钟不动 = 还在防抖窗口内：再变一次也不许立刻重启
  f.write(mkSettings({ ANTHROPIC_MODEL: 'fake-model-x3' }))
  f.w.evaluate()
  expect(f.hits()).toBe(1)
  expect(f.logs).toContain('refresh=false reason=debounced')
  f.tick(10_000)                     // 窗口过去
  f.w.evaluate()
  expect(f.hits()).toBe(2)
})

test('连续快写5次_只重启一次_读到的是最后一次的内容', async () => {
  const f = fixture(mkSettings(), 80)
  f.tick(10_000)
  for (let i = 0; i < 5; i++) {
    f.atomicWrite(mkSettings({ ANTHROPIC_MODEL: `fake-model-burst-${i}` }))
    await sleep(10)
  }
  await sleep(300)                   // 等 fs.watch + 防抖尘埃落定
  expect(f.hits()).toBe(1)
  // 最后一次的内容已成为新基线：再评估不会补第二次
  f.tick(10_000)
  f.w.evaluate()
  expect(f.hits()).toBe(1)
})

test('原子替换后watch不失聪_rename覆盖也能认出变化', async () => {
  const f = fixture(mkSettings(), 60)
  f.tick(10_000)
  f.atomicWrite(mkSettings({ ANTHROPIC_BASE_URL: 'https://api.third-fake.test/anthropic' }))
  await sleep(300)
  expect(f.hits()).toBe(1)           // 没手动 evaluate，全靠 fs.watch 目录事件
})

test('stop之后不再触发', async () => {
  const f = fixture(mkSettings(), 40)
  f.tick(10_000)
  f.w.stop()
  f.write(mkSettings({ ANTHROPIC_MODEL: 'fake-model-after-stop' }))
  f.w.evaluate()
  await sleep(200)
  expect(f.hits()).toBe(0)
})

test('日志与签名都不含token明文', () => {
  const f = fixture()
  f.tick(10_000)
  f.write(mkSettings({ ANTHROPIC_AUTH_TOKEN: 'sk-FAKE-int-zzz999' }))
  f.w.evaluate()
  const all = f.logs.join('\n')
  expect(all.includes(FAKE_TOKEN)).toBe(false)
  expect(all.includes('abc123')).toBe(false)
  expect(all.includes('zzz999')).toBe(false)
  expect(all.includes('fake-provider')).toBe(false)
})

test('settingsPath跟随CLAUDE_SETTINGS_PATH_缺省回落到home下的claude目录', () => {
  const saved = process.env.CLAUDE_SETTINGS_PATH
  try {
    process.env.CLAUDE_SETTINGS_PATH = join(tmpdir(), 'nowhere-fake', 'settings.json')
    expect(settingsPath()).toBe(join(tmpdir(), 'nowhere-fake', 'settings.json'))
    delete process.env.CLAUDE_SETTINGS_PATH
    expect(settingsPath().endsWith(join('.claude', 'settings.json'))).toBe(true)  // 只比路径，不读文件
  } finally {
    if (saved === undefined) delete process.env.CLAUDE_SETTINGS_PATH
    else process.env.CLAUDE_SETTINGS_PATH = saved
  }
})
