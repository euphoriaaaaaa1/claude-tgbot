// inbound_access.ts 单测：群 allowFrom 为空时回退 owner 名单，不再“所有人放行”。
import { describe, expect, test } from 'bun:test'
import { senderAllowed } from './inbound_access'

const G = '-100123'
const OWNER = '111'
const OTHER = '222'

describe('senderAllowed', () => {
  test('群 allowFrom 非空：只认群名单', () => {
    const acc = { allowFrom: [OWNER], groups: { [G]: { allowFrom: [OTHER] } } }
    expect(senderAllowed(acc, 'supergroup', G, OTHER)).toBe(true)
    expect(senderAllowed(acc, 'group', G, OWNER)).toBe(false)
  })

  test('群 allowFrom 为空：回退 owner 名单', () => {
    const acc = { allowFrom: [OWNER], groups: { [G]: { allowFrom: [] } } }
    expect(senderAllowed(acc, 'supergroup', G, OWNER)).toBe(true)
    expect(senderAllowed(acc, 'supergroup', G, OTHER)).toBe(false)
  })

  test('群 allowFrom 与 owner 名单都为空：拒绝', () => {
    const acc = { allowFrom: [], groups: { [G]: { allowFrom: [] } } }
    expect(senderAllowed(acc, 'group', G, OTHER)).toBe(false)
  })

  test('群不在 access.groups 里：拒绝', () => {
    expect(senderAllowed({ allowFrom: [OWNER], groups: {} }, 'group', G, OWNER)).toBe(false)
  })

  test('私聊仍只认 allowFrom', () => {
    expect(senderAllowed({ allowFrom: [OWNER], groups: {} }, 'private', '1', OWNER)).toBe(true)
    expect(senderAllowed({ allowFrom: [OWNER], groups: {} }, 'private', '1', OTHER)).toBe(false)
  })
})
