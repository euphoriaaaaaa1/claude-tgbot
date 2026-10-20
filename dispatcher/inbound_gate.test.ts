// 群放行接线测试：直接调用 dispatcher.ts 导出的 gate()，不是只测 senderAllowed 纯函数。
// 导入前把 HOME / CHANNEL_DIR / Bot API 都指向一次性目录与本机死端口，绝不碰真实服务。
import { afterAll, describe, expect, test } from 'bun:test'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

const BASE = mkdtempSync(join(tmpdir(), 'gate-wire-'))
const HOME = join(BASE, 'home')
const CH = join(BASE, 'channel')
mkdirSync(HOME, { recursive: true })
mkdirSync(CH, { recursive: true })
process.env.HOME = HOME
process.env.CHANNEL_DIR = CH
process.env.BOT_NAME = 'gate-wire'
process.env.TELEGRAM_BOT_TOKEN = '000000:FAKE-TOKEN-FOR-TEST'
process.env.DISPATCHER_TELEGRAM_API_BASE = 'http://127.0.0.1:9'
process.env.DISPATCHER_PORT = '0'

const { gate } = await import('./dispatcher.ts')

afterAll(() => rmSync(BASE, { recursive: true, force: true }))

const OWNER = '111'
const OTHER = '222'
const STRANGER = '333'
const G = '-100123'

type Policy = {
  requireMention: boolean
  allowFrom: string[]
  selfAliases?: string[]
  otherBotUsernames?: string[]
}
function setAccess(allowFrom: string[], policy: Policy | null, mentionPatterns?: string[]): void {
  const groups = policy ? { [G]: policy } : {}
  writeFileSync(join(CH, 'access.json'), JSON.stringify({ allowFrom, groups, mentionPatterns }))
}

function ctx(o: {
  fromId: string
  chatType?: 'private' | 'group' | 'supergroup'
  chatId?: string
  text?: string
  username?: string
  isBot?: boolean
}): any {
  return {
    chat: { id: o.chatId ?? G, type: o.chatType ?? 'supergroup' },
    from: { id: Number(o.fromId), username: o.username, is_bot: o.isBot },
    message: { text: o.text ?? 'hello' },
  }
}

describe('gate 群放行接线', () => {
  test('群 allowFrom 为空 + owner 非空：陌生人文本被拒，owner 通过', () => {
    setAccess([OWNER], { requireMention: false, allowFrom: [] })
    expect(gate(ctx({ fromId: STRANGER })).action).toBe('drop')
    expect(gate(ctx({ fromId: OWNER })).action).toBe('deliver')
  })

  test('群 allowFrom 为空 + owner 也为空：全拒', () => {
    setAccess([], { requireMention: false, allowFrom: [] })
    expect(gate(ctx({ fromId: OWNER })).action).toBe('drop')
    expect(gate(ctx({ fromId: STRANGER })).action).toBe('drop')
  })

  test('群 allowFrom 非空：只放名单，owner 不在名单也拒', () => {
    setAccess([OWNER], { requireMention: false, allowFrom: [OTHER] })
    expect(gate(ctx({ fromId: OTHER })).action).toBe('deliver')
    expect(gate(ctx({ fromId: OWNER })).action).toBe('drop')
    expect(gate(ctx({ fromId: STRANGER })).action).toBe('drop')
  })

  test('peer bot 仍豁免 allowFrom', () => {
    setAccess([], {
      requireMention: false,
      allowFrom: [],
      selfAliases: ['ping'],
      otherBotUsernames: ['@peerbot'],
    })
    expect(gate(ctx({ fromId: '999', username: 'peerbot', isBot: true, text: 'ping' })).action)
      .toBe('deliver')
    // 不在 otherBotUsernames 的 bot 不能搭便车
    expect(gate(ctx({ fromId: '999', username: 'strangerbot', isBot: true, text: 'ping' })).action)
      .toBe('drop')
  })

  test('群不在 access.groups 里：拒', () => {
    setAccess([OWNER], null)
    expect(gate(ctx({ fromId: OWNER })).action).toBe('drop')
  })

  test('私聊仍只认 allowFrom', () => {
    setAccess([OWNER], null)
    expect(gate(ctx({ fromId: OWNER, chatType: 'private', chatId: OWNER })).action).toBe('deliver')
    expect(gate(ctx({ fromId: OTHER, chatType: 'private', chatId: OTHER })).action).toBe('drop')
  })
})
