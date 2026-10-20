// H4b · H-18 供应商变更判定纳入密钥摘要（黑盒验收，bug 修复模式：现在应为红）
//
// 只依据 .devflow/{BRIEF,INTERFACE}-h4b-path-perf.md。不读实现。
// providerSig / startProviderWatch 为本仓既有真实导出；字段名 ANTHROPIC_API_KEY /
// ANTHROPIC_AUTH_TOKEN 取自既有 provider_sync 测试口径。
// 全程假 settings.json + 注入日志/回调，绝不读真实 ~/.claude/settings.json、不打印密钥。
//
// 口径（2026-10-09 裁决，已统一）：provider_sync 签名按五字段口径——BASE_URL / ANTHROPIC_MODEL /
// 顶层 model / ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 一律取不可逆摘要参与。设置文件里的凭据
// 变化 = 供应商变化 → 触发重启；OAuth 续期只改钥匙串、不写设置文件，故设置文件逐字不变时签名不变。
// 详见 provider_sync_sig.test.ts 与 provider_sync_integration.test.ts。
//
// 跑法：bun test tests/acceptance/h4b_path_perf/h18_provider_sig.test.ts
import { test, expect, afterEach } from 'bun:test'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { mkTmp, rmTmp, pickOrThrow, providerSync, providerWatch } from './_fixture.ts'

const DIRS: string[] = []
const LIVE: { stop(): void }[] = []
afterEach(() => {
  while (LIVE.length) LIVE.pop()!.stop()
  for (const d of DIRS.splice(0)) rmTmp(d)
})

const providerSig = (): ((t: string) => string) => pickOrThrow(providerSync, ['providerSig'])
const startProviderWatch = pickOrThrow<(o: Record<string, unknown>) => { stop(): void }>(
  providerWatch, ['startProviderWatch'])

const FAKE_KEY = 'sk-FAKE-key-abc123'
const FAKE_TOKEN = 'sk-FAKE-oauth-token-xyz789'
const BASE_ENV = {
  ANTHROPIC_BASE_URL: 'https://api.fake-provider.test/anthropic',
  ANTHROPIC_MODEL: 'fake-model-x1',
  ANTHROPIC_API_KEY: FAKE_KEY,
  ANTHROPIC_AUTH_TOKEN: FAKE_TOKEN,
}
const mkSettings = (over: Record<string, unknown> = {}) =>
  JSON.stringify({ env: { ...BASE_ENV, ...over }, model: 'fake-top-model' })

// ═══ §9：签名字段扩展纳入 sha256(api_key)[:16] ═══════════════════════════════

test('H18_只换api_key_签名变化', () => {
  const sig = providerSig()
  const before = sig(mkSettings())
  const after = sig(mkSettings({ ANTHROPIC_API_KEY: 'sk-FAKE-key-CHANGED-000' }))
  expect(after).not.toBe(before)
})

test('H18_反向_OAuth续期不写设置文件_签名不变且不触发重启', () => {
  // OAuth 续期只改钥匙串里的凭据，settings.json 逐字不变 → 签名不变 → 不触发重启。
  const sig = providerSig()
  const text = mkSettings()
  // 逐字不变：同一份文件文本前后各算一次签名，必须完全相同
  expect(sig(text)).toBe(sig(text))
  // 反向：签名里不得出现密钥明文（只留摘要）
  expect(sig(text).includes(FAKE_KEY)).toBe(false)
  expect(sig(text).includes('abc123')).toBe(false)

  // 不触发重启：续期只动钥匙串、设置文件一字未改，watch 评估后既无重启也无 provider_changed 日志。
  const dir = mkTmp('h18-'); DIRS.push(dir)
  const file = join(dir, 'settings.json')
  writeFileSync(file, text)
  const logs: string[] = []
  let hits = 0
  let clock = 1_000_000
  const w = startProviderWatch({
    path: file, debounceMs: 60, pollMs: 5_000,
    now: () => clock, log: (l: string) => logs.push(l),
    onProviderChanged: () => { hits++ },
  })
  LIVE.push(w)
  clock += 10_000
  writeFileSync(file, text)   // 原样重写同样字节（模拟续期不写设置文件）
  ;(w as unknown as { evaluate(): void }).evaluate()
  expect(hits).toBe(0)
  expect(logs).not.toContain('refresh=true reason=provider_changed')
})

// ═══ §9：只换密钥 → 触发 provider 变更回调（走 provider_watch）════════════════

test('H18_只换api_key_触发一次重启回调', () => {
  const dir = mkTmp('h18-'); DIRS.push(dir)
  const file = join(dir, 'settings.json')
  writeFileSync(file, mkSettings())
  const logs: string[] = []
  let hits = 0
  let clock = 1_000_000
  const w = startProviderWatch({
    path: file, debounceMs: 60, pollMs: 5_000,
    now: () => clock, log: (l: string) => logs.push(l),
    onProviderChanged: () => { hits++ },
  })
  LIVE.push(w)
  clock += 10_000
  writeFileSync(file, mkSettings({ ANTHROPIC_API_KEY: 'sk-FAKE-key-CHANGED-999' }))
  ;(w as unknown as { evaluate(): void }).evaluate()
  expect(hits).toBe(1)
  expect(logs).toContain('refresh=true reason=provider_changed')
})
