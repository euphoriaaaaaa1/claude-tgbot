// photo_mode.ts 单测：触发/退出解析、状态读写、轮次幂等、投递键。只用临时目录。
import { afterEach, describe, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import {
  CLOCK_SKEW_MS, PHOTO_DELIVERY_KEY_MAX, PHOTO_TTL_MS, deletePhotoState, isPhotoExit,
  nextPhotoRound, normalizePhotoDeliveryKey, parsePhotoStateText, parsePhotoTrigger, photoBotDir,
  photoDeliveryKeyFromMeta, photoExpiresAtMs, photoMeta, photoModeRoot, photoStatePath, readPhotoState, stripWrappingParens,
  topLevelBracketContents, triggerPhotoState, writePhotoState,
} from './photo_mode'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't19-photo-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

describe('触发解析', () => {
  test('精确短语必须整条包在括号里', () => {
    for (const t of ['（拍照）', '(摄像)', '（摄影）', '（每轮拍照）', '（摄像机视角拍照）']) {
      expect(parsePhotoTrigger(t)).toBe(true)
    }
    for (const t of ['拍照', '摄影。', '每轮拍照']) expect(parsePhotoTrigger(t)).toBe(false)
  })

  test('意图词 + 拍照词出现在顶层括号', () => {
    expect(parsePhotoTrigger('来一张（第一视角拍照）')).toBe(true)
    expect(parsePhotoTrigger('（机位拍照）')).toBe(true)
    expect(parsePhotoTrigger('（拍照）')).toBe(true)
  })

  test('普通聊天、嵌套括号、括号外文字不误触', () => {
    for (const t of ['拍照了吗', '我在拍照', '（今天拍照了吗）', '（（第一视角拍照））', '（第一视角）拍照', '']) {
      expect(parsePhotoTrigger(t)).toBe(false)
    }
  })

  test('退出短语', () => {
    for (const t of ['停', '别拍了', '正常拍照', '（停止拍照）', '结束拍照！']) expect(isPhotoExit(t)).toBe(true)
    for (const t of ['停下吗', '拍完了', '不要']) expect(isPhotoExit(t)).toBe(false)
  })

  test('括号工具函数', () => {
    expect(topLevelBracketContents('a（x）b(y)')).toEqual(['x', 'y'])
    expect(topLevelBracketContents('（a（b）c）')).toEqual([])
    expect(stripWrappingParens('（ x ）')).toBe('x')
    expect(stripWrappingParens('（x)')).toBe('（x)')
  })
})

describe('状态文件', () => {
  const now = 1_700_000_000_000
  const state = triggerPhotoState('-100', '42', now)

  test('解析合法状态与 TTL/时钟回拨', () => {
    expect(parsePhotoStateText(JSON.stringify(state), now).status).toBe('ok')
    expect(parsePhotoStateText(JSON.stringify(state), now - CLOCK_SKEW_MS - 1).status).toBe('clock_skew')
    expect(parsePhotoStateText(JSON.stringify(state), now + PHOTO_TTL_MS).status).toBe('expired')
    expect(photoExpiresAtMs(state)).toBe(now + PHOTO_TTL_MS)
    expect(photoMeta(state)).toEqual({ active: true, opened_at_ms: now, expires_at_ms: now + PHOTO_TTL_MS })
  })

  test('坏形状 → unreadable', () => {
    for (const text of ['{', '[]', JSON.stringify({ ...state, v: 2 }), JSON.stringify({ ...state, rounds: -1 }),
      JSON.stringify({ ...state, chat_id: 1 }), JSON.stringify({ ...state, last_delivery_key: 1 })]) {
      expect(parsePhotoStateText(text, now).status).toBe('unreadable')
    }
  })

  test('轮次同 key 幂等，不同 key 递增', () => {
    const a = nextPhotoRound(state, 'k1', now + 10)
    expect(a.rounds).toBe(1)
    expect(a.last_delivery_key).toBe('k1')
    expect(nextPhotoRound(a, 'k1', now + 20)).toBe(a)
    const b = nextPhotoRound(a, 'k2', now + 30)
    expect(b.rounds).toBe(2)
    expect(a.rounds).toBe(1)   // 纯函数，不改旧值
  })

  test('读写删往返', () => {
    const root = mkDir()
    const p = photoStatePath(root, 'bot', '-100')
    expect(readPhotoState(p, now).status).toBe('missing')
    expect(writePhotoState(p, state)).toBe(true)
    expect(readPhotoState(p, now).state?.trigger_message_id).toBe('42')
    expect(deletePhotoState(p)).toBe(true)
    expect(deletePhotoState(p)).toBe(true)
    expect(readPhotoState(p, now).status).toBe('missing')
  })

  test('超 4 KiB 按不可读', () => {
    const root = mkDir()
    mkdirSync(join(root, 'bot'))
    const p = photoStatePath(root, 'bot', '-100')
    writeFileSync(p, 'x'.repeat(5000))
    expect(readPhotoState(p, now).status).toBe('unreadable')
  })

  test('photoModeRoot 覆盖与默认', () => {
    expect(photoModeRoot('/c', { DISPATCHER_PHOTO_MODE_DIR: '/tmp/p' })).toBe('/tmp/p')
    expect(photoModeRoot('/c', { DISPATCHER_PHOTO_MODE_DIR: 'relative' })).toBe('/c/.photo-mode')
  })
})

describe('路径校验（防穿越）', () => {
  test('合法 bot 名与群号正常拼路径', () => {
    expect(photoBotDir('/r', 'bot_1')).toBe('/r/bot_1')
    expect(photoStatePath('/r', 'bot-1', '-100')).toBe('/r/bot-1/-100.json')
    expect(photoStatePath('/r', 'bot', '123')).toBe('/r/bot/123.json')
  })

  test('非法 botName 抛错', () => {
    for (const b of ['../evil', 'a/b', 'a b', '', '.', '..', 'x'.repeat(65), '名字']) {
      expect(() => photoBotDir('/r', b)).toThrow()
      expect(() => photoStatePath('/r', b, '-100')).toThrow()
    }
  })

  test('非法 chatId 抛错', () => {
    for (const c of ['../evil', '/', '', '0', '-01', 'abc', '-1/2', '1e3']) {
      expect(() => photoStatePath('/r', 'bot', c)).toThrow()
    }
  })
})

describe('投递键', () => {
  test('超长 key 取 sha256 前 32 位且稳定', () => {
    const long = 'x'.repeat(PHOTO_DELIVERY_KEY_MAX + 1)
    const h = normalizePhotoDeliveryKey(long)
    expect(h.length).toBe(32)
    expect(h).toBe(normalizePhotoDeliveryKey(long))
    expect(normalizePhotoDeliveryKey('short')).toBe('short')
    expect(normalizePhotoDeliveryKey('')).toBe('')
  })

  test('meta 优先级：merged_message_ids > message_id > ts', () => {
    expect(photoDeliveryKeyFromMeta({ merged_message_ids: [1, 2] })).toBe('1,2')
    expect(photoDeliveryKeyFromMeta({ message_id: 7 })).toBe('7')
    expect(photoDeliveryKeyFromMeta({ ts: 't' })).toBe('t')
    expect(photoDeliveryKeyFromMeta(null)).toBe('')
  })
})
