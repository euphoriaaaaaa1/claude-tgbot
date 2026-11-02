// pub-outage · 内部标记转义与四份标记清单一致性（行为清单 #22）
// 只依据 pub-outage-接口.md 6.4a / 6.5。清单一致性按文本解析四份文件里的方括号项。
import { test, expect } from 'bun:test'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..', '..', '..')
const loadGuard = async (): Promise<any> => await import('../../../dispatcher/chat_guard')

test('escapeSyntheticPrefix_每处内部标记开头的半角方括号变全角', async () => {
  const { escapeSyntheticPrefix } = await loadGuard()
  expect(escapeSyntheticPrefix('看 [hang-check] 和 [resend-check] 吧')).toBe('看 ［hang-check] 和 ［resend-check] 吧')
})

test('escapeSyntheticPrefix_语音图片与同伴bot标记也转义', async () => {
  const { escapeSyntheticPrefix } = await loadGuard()
  expect(escapeSyntheticPrefix('前 [voice-image] 中 [from peer bot x] 后')).toBe('前 ［voice-image] 中 ［from peer bot x] 后')
})

test('escapeSyntheticPrefix_不在清单里的方括号文字原样不变', async () => {
  const { escapeSyntheticPrefix } = await loadGuard()
  for (const s of ['[momentum]', '参考 [1] 和 a[b]c', '没有任何方括号', '', '[ hang-check]', '[HANG-CHECK]']) {
    expect([s, escapeSyntheticPrefix(s)]).toEqual([s, s])
  }
})

test('escapeSyntheticPrefix_入参不是字符串_原样返回同一个值', async () => {
  const { escapeSyntheticPrefix } = await loadGuard()
  const obj = { a: 1 }
  expect(escapeSyntheticPrefix(123)).toBe(123)
  expect(escapeSyntheticPrefix(null)).toBeNull()
  expect(escapeSyntheticPrefix(undefined)).toBeUndefined()
  expect(escapeSyntheticPrefix(obj)).toBe(obj)
})

test('chat_guard导出_补发前缀常量与14项标记清单', async () => {
  const g = await loadGuard()
  expect(g.RESEND_CHECK_PREFIX).toBe('[resend-check]')
  expect(Array.isArray(g.INTERNAL_TEXT_MARKERS)).toBe(true)
  expect(g.INTERNAL_TEXT_MARKERS.length).toBe(14)
  expect(g.INTERNAL_TEXT_MARKERS).toContain('[resend-check]')
  expect(g.INTERNAL_TEXT_MARKERS).toContain('[hang-check]')
  expect(new Set(g.INTERNAL_TEXT_MARKERS).size).toBe(14)
})

test('resend_plan导出_补发常量取值', async () => {
  const rp: any = await import('../../../dispatcher/resend_plan')
  expect(rp.RESEND_PREFIX).toBe('[resend-check]')
  expect(rp.RESEND_SETTLE_MS).toBe(120_000)
  expect(rp.RESEND_AWAIT_MS).toBe(900_000)
  expect(rp.RESEND_TTL_MS).toBe(43_200_000)
  expect(rp.RESEND_MAX_ROUNDS).toBe(3)
})

function sourceOf(file: string): string { return readFileSync(join(REPO, file), 'utf8') }

// 已登记的已知例外（文件 -> 允许缺的标记）；CLI 原生的 [Request interrupted by user 允许缺在任何一份里
const KNOWN_MISSING: Record<string, string[]> = {
  'dispatcher/clear_summary.ts': ['[promise-due'],
}
const CLI_NATIVE = '[Request interrupted by user'
// 过滤清单源码里出现的占位写法（形如 [xxx-due]，不是真标记），文本扫描时跳过
const PLACEHOLDER = /^\[xxx-/

// 「只转义、不进过滤」的标记：同伴 bot 的发言要留在 /clear 摘要和聊天记录里，所以只在转义清单里（锁后修订第 1 条）
const ESCAPE_ONLY = ['[from peer bot']
const FILTER_FILES = ['dispatcher/clear_summary.ts', 'chat_history.py', 'scripts/compact_private_context.py']
// 过滤清单源码里「方括号内部标记」的文本特征：小写连字符词、[moment-、[系统自检、[from peer bot、CLI 原生的 [Request interrupted by user
const MARKER_TOKEN = /\[(?:[a-z]{2,}(?:-[a-z]{2,})+|moment-|系统自检|from peer bot|Request interrupted by user)/g

test('四份标记清单一致_都含resend_check_过滤清单是转义清单的子集_转义多出来的只能是登记过的只转义项', async () => {
  const g = await loadGuard()
  const canon: string[] = (g.INTERNAL_TEXT_MARKERS as string[]).map(m => m.replace(/\]$/, ''))
  expect(canon).toContain('[resend-check')

  const problems: string[] = []
  for (const file of FILTER_FILES) {
    const src = sourceOf(file)
    // 1) 四份清单都含 [resend-check]
    if (!src.includes('[resend-check')) problems.push(`${file} 缺 [resend-check]`)
    // 2) 过滤 ⊆ 转义：过滤清单里出现的内部标记，必须在转义清单里（CLI 原生标记不归 chat_guard 管）
    for (const tok of new Set(src.match(MARKER_TOKEN) ?? [])) {
      if (tok === CLI_NATIVE || PLACEHOLDER.test(tok)) continue
      if (!canon.some(m => tok.startsWith(m))) problems.push(`${file} 里的 ${tok} 不在转义清单里`)
    }
    // 3) 转义比过滤多出来的，只能是登记的只转义项（以及登记过的已知例外）
    for (const m of canon) {
      if (src.includes(m)) continue
      if (ESCAPE_ONLY.includes(m) || (KNOWN_MISSING[file] ?? []).includes(m)) continue
      problems.push(`${file} 缺 ${m}]（转义清单有、过滤清单没有，且未登记为只转义项）`)
    }
  }
  expect(problems).toEqual([])
})

test('四份标记清单一致_只转义项from_peer_bot确实在转义清单里', async () => {
  const g = await loadGuard()
  expect(g.INTERNAL_TEXT_MARKERS).toContain('[from peer bot')
})
