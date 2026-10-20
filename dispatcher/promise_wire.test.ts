// promise_wire.ts 单测：原子写、出站表合并、jsonl 扫描与送达判据。只用临时目录。
import { afterEach, describe, expect, test } from 'bun:test'
import { readFileSync, mkdtempSync, readdirSync, rmSync, statSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import {
  LAST_OUTBOUND_FILE, deliveryEvidence, markOutbound, readLastOutbound, scanJsonlFrom, writeJsonAtomic,
} from './promise_wire'
import { DONE_WINDOW_MS } from './promise_plan'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't19-wire-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

describe('writeJsonAtomic', () => {
  test('写入 0600、无临时文件残留', () => {
    const d = mkDir()
    const p = join(d, 'x.json')
    writeJsonAtomic(p, { a: 1 })
    expect(JSON.parse(readFileSync(p, 'utf8'))).toEqual({ a: 1 })
    expect(statSync(p).mode & 0o777).toBe(0o600)
    expect(readdirSync(d)).toEqual(['x.json'])
  })
})

describe('出站表', () => {
  test('旧数字形态兼容、坏文件按空表', () => {
    const d = mkDir()
    writeFileSync(join(d, LAST_OUTBOUND_FILE), JSON.stringify({ c1: 123, c2: { ms: 456, promise_ids: ['p1', 'bad id'] } }))
    expect(readLastOutbound(d)).toEqual({
      c1: { ms: 123, promise_ids: [] },
      c2: { ms: 456, promise_ids: ['p1'] },
    })
    const e = mkDir()
    writeFileSync(join(e, LAST_OUTBOUND_FILE), '{')
    expect(readLastOutbound(e)).toEqual({})
  })

  test('窗口内合并 promise_ids，窗口外替换；空参数 false', () => {
    const d = mkDir()
    expect(markOutbound(d, 'c1', ['a'], 1000)).toBe(true)
    expect(markOutbound(d, 'c1', ['b'], 1000 + DONE_WINDOW_MS)).toBe(true)
    expect(readLastOutbound(d).c1).toEqual({ ms: 1000 + DONE_WINDOW_MS, promise_ids: ['a', 'b'] })
    expect(markOutbound(d, 'c1', ['c'], 1000 + 3 * DONE_WINDOW_MS)).toBe(true)
    expect(readLastOutbound(d).c1).toEqual({ ms: 1000 + 3 * DONE_WINDOW_MS, promise_ids: ['c'] })
    expect(markOutbound(d, '', ['x'], 1)).toBe(false)
    expect(markOutbound('', 'c', ['x'], 1)).toBe(false)
  })
})

describe('scanJsonlFrom', () => {
  test('从偏移起只回调含 message_id= 的完整行', () => {
    const d = mkDir()
    const p = join(d, 's.jsonl')
    const lines = [
      JSON.stringify({ a: 1 }),
      JSON.stringify({ x: 'message_id="1"' }),
      JSON.stringify({ b: 2 }),
      JSON.stringify({ y: 'message_id="2"' }),
    ]
    writeFileSync(p, lines.join('\n') + '\n')
    const got: string[] = []
    scanJsonlFrom(p, 0, line => got.push(line))
    expect(got.length).toBe(2)
    expect(got[0]).toContain('message_id=')

    const offset = Buffer.byteLength(lines[0] + '\n' + lines[1] + '\n')
    const from1: string[] = []
    scanJsonlFrom(p, offset, line => from1.push(line))
    expect(from1.length).toBe(1)
    expect(from1[0]).toContain('message_id=')
  })

  test('非法偏移从头扫；文件不存在不回调', () => {
    const d = mkDir()
    const p = join(d, 's.jsonl')
    writeFileSync(p, JSON.stringify({ x: 'message_id="1"' }) + '\n')
    const got: string[] = []
    scanJsonlFrom(p, 999_999, line => got.push(line))
    expect(got.length).toBe(1)
    expect(() => scanJsonlFrom(join(d, 'none'), 0, () => { throw new Error('no') })).not.toThrow()
  })
})

describe('deliveryEvidence', () => {
  const key = { chatId: '-100', messageId: '7' }
  const userLine = JSON.stringify({
    origin: { kind: 'channel' },
    message: { content: '<channel chat_id="-100" message_id="7" reply_to_message_id="9">hi</channel>' },
  })
  const enqueueLine = JSON.stringify({
    type: 'queue-operation', operation: 'enqueue',
    content: '<channel chat_id="-100" message_id="7">hi</channel>',
  })
  const absorbedLine = JSON.stringify({
    type: 'queue-operation', operation: 'remove', reason: 'absorbed_mid_turn',
    content: '<channel chat_id="-100" message_id="7">hi</channel>',
  })
  const queuedLine = JSON.stringify({
    type: 'attachment', attachment: { type: 'queued_command', prompt: '<channel chat_id="-100" message_id="7">hi</channel>' },
  })

  test('三种证据分别计数', () => {
    expect(deliveryEvidence([userLine, enqueueLine, absorbedLine, queuedLine].join('\n'), key))
      .toEqual({ user: 1, midTurn: 1, accepted: 1 })
  })

  test('不误中 reply_to_message_id / 别的 chat / 坏行', () => {
    const other = JSON.stringify({
      origin: { kind: 'channel' },
      message: { content: '<channel chat_id="-100" message_id="70">x</channel>' },
    })
    const replyOnly = JSON.stringify({
      origin: { kind: 'channel' },
      message: { content: '<channel chat_id="-100" reply_to_message_id="7">x</channel>' },
    })
    expect(deliveryEvidence([other, replyOnly, '{bad'].join('\n'), key)).toEqual({ user: 0, midTurn: 0, accepted: 0 })
  })

  test('空输入/空键不误判', () => {
    expect(deliveryEvidence('', key)).toEqual({ user: 0, midTurn: 0, accepted: 0 })
    expect(deliveryEvidence(userLine, { chatId: '', messageId: '7' })).toEqual({ user: 0, midTurn: 0, accepted: 0 })
  })
})
