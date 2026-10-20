// botctl.ts 单测：/stopbot /startbot 解析（名单注入）、供应商命令解析、逐步添加流程。
import { describe, expect, test } from 'bun:test'
import {
  ADD_FLOW_TTL_MS, CTL_BOTS, createAddFlows, parseBotCtlWithBots, parseProviderAdd,
  parseProviderCmd, redactProviderAdd,
} from './botctl'

const BOTS = ['alpha', 'beta']

describe('parseBotCtlWithBots', () => {
  test('合法目标、大小写宽容、@mention', () => {
    expect(parseBotCtlWithBots('/stopbot alpha', BOTS)).toEqual({ cmd: 'stopbot', target: 'alpha' })
    expect(parseBotCtlWithBots('/startbot BETA', BOTS)).toEqual({ cmd: 'startbot', target: 'beta' })
    expect(parseBotCtlWithBots('/stopbot@somebot alpha', BOTS)).toEqual({ cmd: 'stopbot', target: 'alpha' })
  })

  test('无参/未知目标/多参数 → usage', () => {
    for (const t of ['/stopbot', '/startbot ', '/stopbot evil', '/stopbot ../x', '/stopbot alpha extra']) {
      const r = parseBotCtlWithBots(t, BOTS)
      expect(r && 'usage' in r ? r.usage : '').toContain('用法')
    }
  })

  test('空名单：任何目标都只给用法，不放行', () => {
    expect(parseBotCtlWithBots('/stopbot alpha', [])).toMatchObject({ usage: expect.stringContaining('尚未配置') })
  })

  test('不相关命令 → null', () => {
    expect(parseBotCtlWithBots('/clear', BOTS)).toBeNull()
    expect(parseBotCtlWithBots('stopbot alpha', BOTS)).toBeNull()
  })

  test('默认名单来自 DISPATCHER_CTL_BOTS（测试环境未设时为空）', () => {
    expect(Array.isArray(CTL_BOTS)).toBe(true)
  })
})

describe('供应商命令解析', () => {
  test('/model 与 /provider 只取首行、截 200 字', () => {
    expect(parseProviderCmd('/model')).toEqual({ kind: 'model', arg: '' })
    expect(parseProviderCmd('/provider relay\n第二行')).toEqual({ kind: 'provider', arg: 'relay' })
    expect(parseProviderCmd('/provider ' + 'x'.repeat(300))?.arg.length).toBe(200)
    expect(parseProviderCmd('/provider add x')).toBeNull()
    expect(parseProviderCmd('/clear')).toBeNull()
  })

  test('/provider add 预填与超长用法', () => {
    expect(parseProviderAdd('/provider add')).toEqual({ prefill: [] })
    expect(parseProviderAdd('/provider add n u k')).toEqual({ prefill: ['n', 'u', 'k', 'anthropic'] })
    expect(parseProviderAdd('/provider add n u k openai')).toEqual({ prefill: ['n', 'u', 'k', 'openai'] })
    expect(parseProviderAdd('/provider add a b c d e')).toMatchObject({ usage: expect.any(String) })
    expect(parseProviderAdd('/provider list')).toBeNull()
  })

  test('日志脱敏只保留 add 头', () => {
    expect(redactProviderAdd('/provider add secret-key')).toBe('/provider add <参数已隐去>')
    expect(redactProviderAdd('/provider relay')).toBe('/provider relay')
  })
})

describe('createAddFlows', () => {
  function mk() {
    let t = 1_000_000
    const sent: string[] = []
    const deleted: number[] = []
    const io = {
      send: async (_chat: string, text: string) => { sent.push(text) },
      del: async (_chat: string, id: number) => { deleted.push(id); return true },
      isTaken: async (name: string) => name === 'taken',
      run: async () => '已添加',
      now: () => t,
    }
    return { flows: createAddFlows(io), sent, deleted, tick: (ms: number) => { t += ms } }
  }

  test('逐步提问、四步完成后自然结束', async () => {
    const { flows, sent } = mk()
    await flows.start('c1', [])
    expect(flows.pendingStep('c1')).toBe('name')
    expect(sent.at(-1)).toContain('第 1/4 步')
    await flows.onMessage('c1', { text: 'relay', isText: true, messageId: 1 })
    expect(flows.pendingStep('c1')).toBe('baseUrl')
    await flows.onMessage('c1', { text: 'https://x.example/v1', isText: true, messageId: 2 })
    await flows.onMessage('c1', { text: 'AbCdEfG8', isText: true, messageId: 3 })
    expect(flows.pendingStep('c1')).toBe('protocol')
    await flows.onMessage('c1', { text: '1', isText: true, messageId: 4 })
    expect(flows.isActive('c1')).toBe(false)
    expect(sent.some(s => s.includes('正在添加'))).toBe(true)
  })

  test('等待密钥时先删原消息', async () => {
    const { flows, deleted } = mk()
    await flows.start('c1', ['relay', 'https://x.example/v1'])
    await flows.onMessage('c1', { text: 'secret', isText: true, messageId: 9 })
    expect(deleted).toEqual([9])
  })

  test('取消与 TTL、abort、会话隔离', async () => {
    const { flows, tick } = mk()
    await flows.start('c1', [])
    await flows.onMessage('c1', { text: '/cancel', isText: true })
    expect(flows.isActive('c1')).toBe(false)

    await flows.start('c2', [])
    tick(ADD_FLOW_TTL_MS + 1)
    expect(flows.isActive('c2')).toBe(false)

    await flows.start('c3', [])
    flows.abort('c3')
    expect(flows.isActive('c3')).toBe(false)
    expect(await flows.onMessage('c3', { text: '普通消息', isText: true })).toBe(false)
    expect(flows.isActive('c4')).toBe(false)
  })

  test('重名当场拒绝；非文字步骤不推进', async () => {
    const { flows, sent } = mk()
    await flows.start('c1', ['taken'])
    expect(sent.at(-1)).toContain('已经有叫')
    await flows.start('c2', [])
    await flows.onMessage('c2', { text: '', isText: false })
    expect(flows.pendingStep('c2')).toBe('name')
    expect(sent.at(-1)).toContain('这一步要发文字')
  })
})
