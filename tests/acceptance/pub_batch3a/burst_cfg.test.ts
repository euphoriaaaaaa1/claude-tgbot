// pub-batch3a · readBurstCfg 缺省与取值规则（行为清单 14、15）。
// 只经公开函数 readBurstCfg(dir) 与 createBurstCollector(...).enabled 观察；配置目录是临时目录里的 access.json。
import { afterAll, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createBurstCollector, readBurstCfg } from '../../../dispatcher/burst_inbox'

const ROOT = mkdtempSync(join(tmpdir(), 'pb3a-cfg-'))
afterAll(() => { if (ROOT.startsWith(tmpdir())) rmSync(ROOT, { recursive: true, force: true }) })

let seq = 0
function dirWith(access?: unknown | { raw: string }): string {
  const d = join(ROOT, `d${++seq}`)
  mkdirSync(d, { recursive: true })
  if (access !== undefined) {
    const raw = typeof access === 'object' && access !== null && 'raw' in access ? (access as any).raw : JSON.stringify(access)
    writeFileSync(join(d, 'access.json'), raw)
  }
  return d
}
const cfgOf = (o: Record<string, unknown>) => readBurstCfg(dirWith(o))

test('14_文件缺失_JSON损坏_或缺这两个键_都返回窗口8000_封顶0', () => {
  const want = { windowMs: 8000, maxMs: 0 }
  expect(readBurstCfg(dirWith())).toEqual(want)                       // 没有 access.json
  expect(readBurstCfg(dirWith({ raw: '{not json' }))).toEqual(want)   // JSON 坏
  expect(readBurstCfg(dirWith({ allowFrom: ['FAKE'] }))).toEqual(want) // 有文件但没这两个键
})

test('15a_burstMaxMs_0和负数都得0_小于窗口时抬到窗口_字符串数字可用_非数回落0', () => {
  expect(cfgOf({ burstWindowMs: 8000, burstMaxMs: 0 }).maxMs).toBe(0)
  expect(cfgOf({ burstWindowMs: 8000, burstMaxMs: -5 }).maxMs).toBe(0)
  expect(cfgOf({ burstWindowMs: 8000, burstMaxMs: 1000 }).maxMs).toBe(8000)
  expect(cfgOf({ burstWindowMs: 8000, burstMaxMs: '9000' }).maxMs).toBe(9000)
  expect(cfgOf({ burstWindowMs: 8000, burstMaxMs: 'abc' }).maxMs).toBe(0)
  expect(cfgOf({ burstWindowMs: 8000, burstMaxMs: null }).maxMs).toBe(0)
})

test('15b_burstWindowMs_负数关闭攒波_非数回落8000_字符串数字可用', () => {
  const collector = (windowMs: number) => createBurstCollector({
    windowMs, maxMs: 0, destDir: join(ROOT, 'never-written'), now: () => 1, deliver: () => {},
  })
  expect(collector(cfgOf({ burstWindowMs: -1 }).windowMs).enabled).toBe(false)
  expect(cfgOf({ burstWindowMs: 'abc' }).windowMs).toBe(8000)
  expect(cfgOf({ burstWindowMs: '3000' }).windowMs).toBe(3000)
})
