// voice_bridge_auth.ts 单测：令牌文件缺失时不带鉴权头（让请求失败），存在时带 Bearer。
import { afterEach, describe, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { voiceBridgeAuthHeaders } from './voice_bridge_auth'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't-vbauth-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

describe('voiceBridgeAuthHeaders', () => {
  test('读 VOICE_BRIDGE_TOKEN_FILE 并带 Bearer', () => {
    const d = mkDir()
    const f = join(d, 'voice-bridge.token')
    writeFileSync(f, 'deadbeef\n')
    expect(voiceBridgeAuthHeaders({ VOICE_BRIDGE_TOKEN_FILE: f }, p => readFileSync(p, 'utf8'), d))
      .toEqual({ Authorization: 'Bearer deadbeef' })
  })

  test('文件缺失 → 空头，不回退无鉴权', () => {
    const d = mkDir()
    expect(voiceBridgeAuthHeaders({ VOICE_BRIDGE_TOKEN_FILE: join(d, 'nope') }, p => readFileSync(p, 'utf8'), d))
      .toEqual({})
  })

  test('默认路径可用 ~ 展开', () => {
    const d = mkDir()
    mkdirSync(join(d, '.claude', 'secrets'), { recursive: true })
    writeFileSync(join(d, '.claude', 'secrets', 'voice-bridge.token'), 'abc')
    expect(voiceBridgeAuthHeaders({}, p => readFileSync(p, 'utf8'), d))
      .toEqual({ Authorization: 'Bearer abc' })
  })

  test('空文件 → 空头', () => {
    const d = mkDir()
    const f = join(d, 'voice-bridge.token')
    writeFileSync(f, '   \n')
    expect(voiceBridgeAuthHeaders({ VOICE_BRIDGE_TOKEN_FILE: f }, p => readFileSync(p, 'utf8'), d))
      .toEqual({})
  })
})
