// H3b · H-17 整条请求耗时上界（SEND_TOTAL_BUDGET_MS=60000）—— 黑盒验收（慢测，子进程隔离）
// 只依据 .devflow/INTERFACE-h3b-delivery.md 四(16)。不读实现。
// 让第一个段永不返回：到界应停止后续段、把未发段以 reason=timeout 记入 dropped 并按 §12 返回。
// 现状无上界 → 子进程里请求会一直挂住（被 66s 取消）→ returned=false → 本测为红。
// 隔离：挂住发生在子进程，绝不污染同进程的其它测试文件。
import { test, expect } from 'bun:test'
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const DRIVER = join(dirname(fileURLToPath(import.meta.url)), '_budget_driver.ts')

test('总预算到界_停止后续段_未发段以timeout记入dropped', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'h3b-budget-spec-'))
  const out = join(dir, 'out.json')
  const proc = Bun.spawn([process.execPath, DRIVER, out], { stdout: 'ignore', stderr: 'ignore', env: { ...process.env } })
  const killer = setTimeout(() => { try { proc.kill() } catch { /* ignore */ } }, 90000)
  await proc.exited
  clearTimeout(killer)
  const res: any = existsSync(out) ? JSON.parse(readFileSync(out, 'utf8')) : { returned: false }
  expect(res.returned).toBe(true)                 // 必须封顶返回，不得无限等待
  expect(res.elapsed).toBeLessThan(65000)         // 上界附近返回
  expect(res.status).toBe(502)                    // 全丢（sent=0）
  expect(res.body?.planned).toBe(2)
  expect(res.body?.sent).toBe(0)
  const reasons = (res.body?.dropped ?? []).map((d: any) => d.reason)
  expect(reasons.length).toBe(2)
  expect(reasons.every((x: string) => x === 'timeout')).toBe(true)
}, 95000)
