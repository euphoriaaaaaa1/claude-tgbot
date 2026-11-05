// pub-batch3a · testModeCheck 的端口拦截范围（行为清单 24、25）。
// 只经公开纯函数 testModeCheck(env)：它只读传入的 env，不读 process.env，不连任何端口。
// 已知局限（接口文件写明）：yml 或环境变量自定义的端口不在拦截范围内，这批用例不测。
import { afterAll, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { testModeCheck } from '../../../dispatcher/chat_guard'

const ROOT = realpathSync(mkdtempSync(join(tmpdir(), 'pb3a-ports-')))
const HOME = join(ROOT, 'home')
const CHANNEL_DIR = join(ROOT, 'channels', 'alpha')
for (const d of [HOME, CHANNEL_DIR]) mkdirSync(d, { recursive: true })
afterAll(() => { if (ROOT.startsWith(realpathSync(tmpdir()))) rmSync(ROOT, { recursive: true, force: true }) })

const envWithPort = (port: number, testMode = true) => ({
  ...(testMode ? { CLAUDEBOTLIFE_TEST: '1' } : {}),
  CLAUDEBOTLIFE_TEST_ROOT: ROOT, HOME, CHANNEL_DIR,
  TELEGRAM_DISPATCHER_URL: `http://127.0.0.1:${port}`,
})
const FORBIDDEN = { ok: false, reason: 'forbidden_port' }
const OK = { ok: true, reason: null }

test('24_测试模式下_17805和17899都落在17801到17899段内_forbidden_port', () => {
  expect(testModeCheck(envWithPort(17805))).toEqual(FORBIDDEN)
  expect(testModeCheck(envWithPort(17899))).toEqual(FORBIDDEN)
})

test('25a_测试模式下_17800_17900_17990在段外_通过', () => {
  for (const port of [17800, 17900, 17990]) expect(testModeCheck(envWithPort(port))).toEqual(OK)
})

test('25b_测试模式下_7897_7788_17801仍被拒', () => {
  for (const port of [7897, 7788, 17801]) expect(testModeCheck(envWithPort(port))).toEqual(FORBIDDEN)
})

test('25c_非测试模式下_17805不拦（生产零影响）', () => {
  expect(testModeCheck(envWithPort(17805, false))).toEqual(OK)
})
