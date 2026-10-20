// H3a-H15 本机 HTTP 接口鉴权 —— 黑盒验收（bug 修复模式：当前应为红，修好后转绿）
// 只依据 .devflow/INTERFACE-h3a-security.md 三(9-15)。不读实现。
// 起一个 127.0.0.1 上的临时 HTTP 服务测鉴权；拒绝路径断言通道目录零写盘。绝不发真 Telegram 请求。
import { test, expect, beforeAll, afterAll } from 'bun:test'
import { chmodSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import {
  startTestServer, getServerFactory, removeToken, tokenPath, readToken,
  tokenHeaders, wrongHeaders, snapshotDir, CHANNEL_DIR, cleanupBase,
} from './_fixtures.ts'

let srv: Awaited<ReturnType<typeof startTestServer>> = null
let setupErr = ''
beforeAll(async () => {
  removeToken()
  try {
    srv = await startTestServer()
    if (!srv) setupErr = '未发现可独立启动的本机 HTTP 服务工厂（实现须导出，契约未列名，夹具按候选名找）'
  } catch (e: any) { setupErr = String(e?.message ?? e) }
})
afterAll(() => { srv?.stop(); srv = null; cleanupBase() })
function need(): NonNullable<typeof srv> {
  if (setupErr) throw new Error(`HTTP 夹具不可用: ${setupErr}`)
  if (!srv) throw new Error('HTTP 夹具不可用')
  return srv
}

// ── 契约 10：默认拒绝 —— 未在契约中列出的路径，无口令必须 401（fail-closed 关键证据）──
test('H15_未列出路径_无口令_401', async () => {
  const s = need()
  const r = await fetch(`${s.base}/whatever-new`, { method: 'GET' })
  expect(r.status).toBe(401)
})

// ── 契约 10：已知路径无口令同样 401 ────────────────────────────────────────
test('H15_已知路径_无口令_401', async () => {
  const s = need()
  const r = await fetch(`${s.base}/status`, { method: 'GET' })
  expect(r.status).toBe(401)
})

// ── 契约 10/11：错口令 → 401 ───────────────────────────────────────────────
test('H15_错误口令_401', async () => {
  const s = need()
  const r = await fetch(`${s.base}/status`, { headers: wrongHeaders() })
  expect(r.status).toBe(401)
})

// ── 契约 11：正确口令 → 与既有状态码一致（非 401/403）───────────────────────
test('H15_正确口令_放行', async () => {
  const s = need()
  const r = await fetch(`${s.base}/status`, { headers: tokenHeaders(s.token) })
  expect(r.status).not.toBe(401)
  expect(r.status).not.toBe(403)
})

// ── 反向对照：显式放行的 /health 无口令也不拒（证明不是"一律拒绝"的空实现）──
test('H15_健康检查_无口令_放行', async () => {
  const s = need()
  const r = await fetch(`${s.base}/health`)
  expect(r.status).not.toBe(401)
  expect(r.status).not.toBe(403)
})

// ── 契约 11：跨源 Origin → 403，且优先于口令（带正确口令仍 403）─────────────
test('H15_跨源Origin_403_即便带正确口令', async () => {
  const s = need()
  const withTok = await fetch(`${s.base}/status`, { headers: { ...tokenHeaders(s.token), Origin: 'https://evil.example' } })
  expect(withTok.status).toBe(403)
  const noTok = await fetch(`${s.base}/status`, { headers: { Origin: 'https://evil.example' } })
  expect(noTok.status).toBe(403)
})

// ── 契约 11：同源 Origin 放行 ──────────────────────────────────────────────
test('H15_同源Origin_带口令_放行', async () => {
  const s = need()
  const r = await fetch(`${s.base}/status`, { headers: { ...tokenHeaders(s.token), Origin: s.base } })
  expect(r.status).not.toBe(401)
  expect(r.status).not.toBe(403)
})

// ── 契约 11：写方法非 application/json → 415 ───────────────────────────────
test('H15_写方法非JSON_415', async () => {
  const s = need()
  const r = await fetch(`${s.base}/status`, {
    method: 'POST', headers: { ...tokenHeaders(s.token), 'content-type': 'text/plain' }, body: 'hello',
  })
  expect(r.status).toBe(415)
})

// ── 契约 13：401/403/415 路径零写盘 ────────────────────────────────────────
test('H15_拒绝路径_零写盘', async () => {
  const s = need()
  const before = snapshotDir(CHANNEL_DIR)
  await fetch(`${s.base}/whatever-new`)                                       // 401
  await fetch(`${s.base}/status`, { headers: wrongHeaders() })                // 401
  await fetch(`${s.base}/status`, { headers: { Origin: 'https://evil.example' } })  // 403
  await fetch(`${s.base}/status`, { method: 'POST', headers: { ...tokenHeaders(s.token), 'content-type': 'text/plain' }, body: 'x' }) // 415
  expect(snapshotDir(CHANNEL_DIR)).toEqual(before)
})

// ── 契约 9：口令文件 0600、内容长度 ≥ 32 ───────────────────────────────────
test('H15_口令文件_权限0600_长度至少32', async () => {
  need()
  const st = statSync(tokenPath())
  expect(st.mode & 0o777).toBe(0o600)
  expect(readToken().length).toBeGreaterThanOrEqual(32)
})

// ── 契约 14/15：口令文件权限不符 → 拒绝启动（fail-closed，不得降级无鉴权）──
test('H15_口令文件权限不符_拒绝启动', async () => {
  const f = await getServerFactory()
  if (!f) throw new Error('未发现可独立启动的本机 HTTP 服务工厂')
  removeToken()
  writeFileSync(tokenPath(), 'Z'.repeat(40)); chmodSync(tokenPath(), 0o644)   // 预置不安全权限
  let started = false; let stop: (() => void) | undefined
  for (const arg of [{ port: 0 }, 0, undefined]) {
    try {
      const r: any = await f(arg)
      const s = r?.server ?? r
      if (s && typeof s.stop === 'function' && Number(s.port ?? r?.port) > 0) { started = true; stop = () => s.stop(true) }
      break
    } catch { break }                                                          // 抛错=拒绝启动，符合契约
  }
  stop?.()
  removeToken()
  expect(started).toBe(false)
})
