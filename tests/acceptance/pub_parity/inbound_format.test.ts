// pub-parity · 入站格式固定（行为清单 21 的 TS 侧，审查 I6a）
// buildContent 对下面这份固定输入、固定时刻的输出，必须与仓内样例 tests/fixtures/inbound_private.txt 一致
// （样例由实现按"这份输入"生成；Python 侧的压缩补回测试读同一份样例，哪一边改了格式都会红）。
// 固定输入：私聊，chat_id=user_id=111222333，名字 alice，ts=2026-10-24T08:30:00.000Z，正文"你好，今天天气不错"，当前时刻固定为 08:31:00Z。
import { test, expect, setSystemTime, afterAll } from 'bun:test'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const BASE = mkdtempSync(join(tmpdir(), 'pp-inbound-'))
const botDir = join(BASE, 'channels', 'bot1')
mkdirSync(botDir, { recursive: true }); mkdirSync(join(BASE, 'home'), { recursive: true })
Object.assign(process.env, { HOME: join(BASE, 'home'), CHANNEL_DIR: botDir, BOT_NAME: 'bot1', TELEGRAM_WORKER_BOT: 'bot1',
  TELEGRAM_WORKER_CHAT_ID: '111222333', TELEGRAM_DISPATCHER_URL: 'http://127.0.0.1:9', TELEGRAM_BOT_TOKEN: 'FAKE-TOKEN-DO-NOT-LEAK' })
afterAll(() => { setSystemTime(); try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } })

const { WorkerManager } = await import('../../../dispatcher/worker-manager')
const SAMPLE = join(import.meta.dir, '..', '..', 'fixtures', 'inbound_private.txt')
const META = { chat_id: '111222333', message_id: 7, from_id: '111222333', from_username: 'alice', user: 'alice', scene: 'private',
  chat_type: 'private', ts: '2026-10-24T08:30:00.000Z', text: '你好，今天天气不错' }

function build(): string {
  setSystemTime(new Date('2026-10-24T08:31:00.000Z'))
  const m = Object.create(WorkerManager.prototype) as any       // 与 worker_manager_stale.test.ts 同款最小实例
  m.inFlight = null; m.phaseVal = 'running'; m.queue = []
  return String(m.buildContent({ ...META }))
}

test('buildContent_固定输入固定时刻_输出与样例文件一致', () => {
  expect(existsSync(SAMPLE)).toBe(true)                          // 样例由实现提供
  expect(build().trimEnd()).toBe(readFileSync(SAMPLE, 'utf8').trimEnd())
})

test('buildContent_私聊入站形态_前缀在开标签之前并以私聊标注结尾_正文在标签内_以闭标签结尾', () => {
  const out = build().trimEnd()
  expect(out.endsWith('</channel>')).toBe(true)
  const i = out.lastIndexOf('<channel')
  expect(i).toBeGreaterThan(0)                                   // 关系块/时刻行/场景标注等在开标签之前
  expect(out.slice(0, i).trimEnd().endsWith('【私聊】')).toBe(true)
  const open = out.slice(i, out.indexOf('>', i) + 1)
  expect(open).toContain('chat_id="111222333"'); expect(open).toContain('chat_type="private"')
  expect(out.slice(out.indexOf('>', i) + 1, -'</channel>'.length)).toContain(META.text)
  expect(out.slice(0, i)).not.toContain(META.text)               // 正文不在前缀里
})
