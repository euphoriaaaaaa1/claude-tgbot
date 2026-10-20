// H3a-H11 worker 环境白名单 —— 黑盒验收（bug 修复模式：当前应为红，修好后转绿）
// 只依据 .devflow/INTERFACE-h3a-security.md 一(1-4)。不读实现。
// 纯函数测试：只喂一个"源环境对象"，观察返回对象里有没有/缺不缺某些键。
import { test, expect, afterAll } from 'bun:test'
import {
  loadWorkerManager, pickFn, WORKER_ENV_NAMES, REQUIRED_WORKER_ENV, OPTIONAL_WORKER_ENV,
  FORBIDDEN_WORKER_ENV, cleanupBase,
} from './_fixtures.ts'

afterAll(cleanupBase)

const wm: any = await loadWorkerManager()
const workerEnv: any = pickFn(wm, WORKER_ENV_NAMES)

/** 起一个"齐全且含敏感键"的源环境。 */
function fullSource(): Record<string, string> {
  const s: Record<string, string> = {}
  for (const k of REQUIRED_WORKER_ENV) s[k] = `val-${k}`
  s.ANTHROPIC_AUTH_TOKEN = 'FAKE-ANTHROPIC-AUTH-DO-NOT-LEAK'
  for (const k of OPTIONAL_WORKER_ENV) s[k] = `opt-${k}`
  s.TELEGRAM_BOT_TOKEN = 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK'
  s.PEERS_JSON = '{"peers":["secret"]}'
  s.ANTHROPIC_API_KEY = 'sk-ant-LEAK-DO-NOT-APPEAR'
  s.TOP_SECRET_UNLISTED = 'UNLISTED-LEAK-DO-NOT-APPEAR'
  return s
}

function need(): void {
  if (typeof workerEnv !== 'function') throw new Error(`未发现 workerEnv 导出（候选名 ${WORKER_ENV_NAMES.join('/')}）`)
}

// ── 契约 2：令牌类 + 未列出键，一个都不能出现在返回里 ──────────────────────
test('H11_令牌与未列出键_一律不出现', async () => {
  need()
  const out = await workerEnv(fullSource())
  for (const k of FORBIDDEN_WORKER_ENV) expect(Object.prototype.hasOwnProperty.call(out, k)).toBe(false)
  // 反向锚点：源里确实有这些键（否则是空转）
  const src = fullSource()
  for (const k of FORBIDDEN_WORKER_ENV) expect(k in src).toBe(true)
})

// ── 契约 3：必填键逐个出现且值一致 ─────────────────────────────────────────
test('H11_必填键_逐个出现且值一致', async () => {
  need()
  const src = fullSource()
  const out = await workerEnv(src)
  for (const k of REQUIRED_WORKER_ENV) {
    expect(Object.prototype.hasOwnProperty.call(out, k)).toBe(true)
    expect(out[k]).toBe(src[k])
  }
})

// ── 契约 3：可选键当且仅当源里有才出现 ────────────────────────────────────
test('H11_可选键_源有则出现_源无则不出现', async () => {
  need()
  const withOpt = await workerEnv(fullSource())
  for (const k of OPTIONAL_WORKER_ENV) expect(Object.prototype.hasOwnProperty.call(withOpt, k)).toBe(true)

  const noOpt: Record<string, string> = {}
  for (const k of REQUIRED_WORKER_ENV) noOpt[k] = `v-${k}`
  const out = await workerEnv(noOpt)
  for (const k of OPTIONAL_WORKER_ENV) expect(Object.prototype.hasOwnProperty.call(out, k)).toBe(false)
})

// ── 契约 3/4：缺必填 → 启动失败路径，且不泄露任何值 ────────────────────────
test('H11_缺必填_失败且不泄露任何值', async () => {
  need()
  const src = fullSource()
  delete src.ANTHROPIC_AUTH_TOKEN
  let threw = false, msg = ''
  try { await workerEnv(src) } catch (e: any) { threw = true; msg = String(e?.message ?? e) }
  expect(threw).toBe(true)                                       // 缺必填必须 fail-closed
  expect(msg).toContain('ANTHROPIC_AUTH_TOKEN')                  // 缺哪个键要说清（只记键名）
  // 不得把任何"值"打出来
  for (const leak of ['FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK', 'FAKE-ANTHROPIC-AUTH-DO-NOT-LEAK', 'sk-ant-LEAK-DO-NOT-APPEAR']) {
    expect(msg).not.toContain(leak)
  }
})

// ── 边界：空源 → 全缺 → 同样 fail-closed ──────────────────────────────────
test('H11_空源_全缺必填_失败', async () => {
  need()
  let threw = false
  try { await workerEnv({}) } catch { threw = true }
  expect(threw).toBe(true)
})
