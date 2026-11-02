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

test('四份标记清单一致_chat_guard的14项都出现在另外三份里_缺的只能是登记过的已知例外', async () => {
  const g = await loadGuard()
  const canon: string[] = (g.INTERNAL_TEXT_MARKERS as string[]).map(m => m.replace(/\]$/, ''))
  expect(canon).toContain('[resend-check')
  const problems: string[] = []
  for (const file of ['dispatcher/clear_summary.ts', 'chat_history.py', 'scripts/compact_private_context.py']) {
    const src = sourceOf(file)
    for (const m of canon) {
      if (src.includes(m)) continue
      if (m === CLI_NATIVE || (KNOWN_MISSING[file] ?? []).includes(m)) continue
      problems.push(`${file} 缺 ${m}]`)
    }
  }
  expect(problems).toEqual([])
})
