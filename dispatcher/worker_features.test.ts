// worker_features.ts 单测：功能关闭提示与 reply 工具描述。纯函数。
import { describe, expect, test } from 'bun:test'
import { FEATURE_KEYS } from './features'
import { featuresOffRule, replyDescription } from './worker_features'

const flags = (off: string[]) =>
  Object.fromEntries(FEATURE_KEYS.map(k => [k, !off.includes(k)])) as never

describe('featuresOffRule', () => {
  test('全开 → 空串', () => {
    expect(featuresOffRule(flags([]))).toBe('')
  })

  test('按固定顺序列中文名', () => {
    const text = featuresOffRule(flags(['image', 'life']))
    expect(text).toBe('这只 bot 现在关掉了这些功能：生图、生活作息。即使人设或技能里提到，也不要用；对方要的话，照实说现在用不了。')
  })

  test('缺键按关处理', () => {
    expect(featuresOffRule({ voice: true } as never)).toContain('生图')
  })
})

describe('replyDescription', () => {
  test('语音开：保留语音参数与 voice_text 提醒', () => {
    const text = replyDescription(true)
    expect(text).toContain('as_voice、voice_text、voice_emotion、voice_instruct')
    expect(text).toContain('标记不要写进 voice_text')
  })

  test('语音关：去掉语音参数，保留 [[图N]] 说明', () => {
    const text = replyDescription(false)
    expect(text).not.toContain('as_voice')
    expect(text).not.toContain('voice_text')
    expect(text).toContain('[[图1]]')
  })
})
