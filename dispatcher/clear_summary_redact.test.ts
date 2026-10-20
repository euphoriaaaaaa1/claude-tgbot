// 黑盒验收：clear_summary.ts 的 redactSecrets / mechanicalDigest
import { test, expect } from 'bun:test'
import { redactSecrets, mechanicalDigest } from './clear_summary'

const TG = `123456789:FAKE-${'a'.repeat(30)}`
const turn = (role: 'user' | 'assistant', text: string) => ({ role, text })

/* ---------------- redactSecrets ---------------- */
test('redactSecrets_电报bot令牌形态_被替换且原串不再出现', () => {
  const out = redactSecrets(`令牌是 ${TG} 记一下`)
  expect(out).toContain('[redacted]')
  expect(out).not.toContain(TG)
})

test('redactSecrets_冒号后不足30位_不算令牌不脱敏', () => {
  const s = `123456789:FAKE-${'a'.repeat(20)}`
  expect(redactSecrets(s)).toBe(s)
})

test('redactSecrets_冒号前只有7位数字_不算令牌不脱敏', () => {
  const s = `1234567:FAKE-${'a'.repeat(30)}`
  expect(redactSecrets(s)).toBe(s)
})

test('redactSecrets_sk开头长串_被脱敏', () => {
  expect(redactSecrets(`key=sk-${'x'.repeat(20)}`)).toContain('[redacted]')
})

test('redactSecrets_sk开头但不足16位_不脱敏', () => {
  const s = `sk-${'x'.repeat(10)}`
  expect(redactSecrets(s)).toBe(s)
})

test('redactSecrets_sk_ant前缀_被脱敏', () => {
  const out = redactSecrets('sk-ant-fake123')
  expect(out).toContain('[redacted]')
  expect(out).not.toContain('fake123')
})

test('redactSecrets_ghp前缀_被脱敏', () => {
  expect(redactSecrets('ghp-faketoken123')).toContain('[redacted]')
})

test('redactSecrets_gho前缀_被脱敏', () => {
  expect(redactSecrets('gho-faketoken123')).toContain('[redacted]')
})

test('redactSecrets_32位纯十六进制串_被脱敏', () => {
  expect(redactSecrets(`hash ${'deadbeef'.repeat(4)}`)).toContain('[redacted]')
})

test('redactSecrets_31位十六进制串_未达长度门槛不脱敏', () => {
  const s = `hash ${'deadbeef'.repeat(4).slice(0, 31)}`
  expect(redactSecrets(s)).toBe(s)
})

test('redactSecrets_带引号的chat_id_被脱敏', () => {
  const out = redactSecrets('来源 chat_id="FAKECHAT-1" 结束')
  expect(out).toContain('[redacted]')
  expect(out).not.toContain('FAKECHAT-1')
})

test('redactSecrets_不带引号的chat_id_被脱敏', () => {
  expect(redactSecrets('chat_id=FAKECHAT-1')).not.toContain('FAKECHAT-1')
})

test('redactSecrets_一段里两处敏感值_全部替换', () => {
  const out = redactSecrets(`${TG} 和 sk-${'x'.repeat(20)}`)
  expect(out.split('[redacted]').length - 1).toBeGreaterThanOrEqual(2)
})

test('redactSecrets_普通中文没有命中_原样返回', () => {
  const s = '今天聊了做饭和散步，还说了周末计划'
  expect(redactSecrets(s)).toBe(s)
})

test('redactSecrets_脱敏不吃掉周围正文', () => {
  const out = redactSecrets(`前面的话 ${TG} 后面的话`)
  expect(out).toContain('前面的话')
  expect(out).toContain('后面的话')
})

test('redactSecrets_传入null_返回空串不抛', () => {
  expect(redactSecrets(null as any)).toBe('')
})

test('redactSecrets_传入undefined_返回空串不抛', () => {
  expect(redactSecrets(undefined as any)).toBe('')
})

test('redactSecrets_传入数字_返回空串不抛', () => {
  expect(redactSecrets(123 as any)).toBe('')
})

/* ---------------- mechanicalDigest（模型摘要失败时的降级链） ---------------- */
test('mechanicalDigest_两轮对话_每轮一行且以短横线和角色标签开头', () => {
  const lines = mechanicalDigest([turn('user', '甲说的话'), turn('assistant', '乙说的话')]).trim().split('\n')
  expect(lines).toHaveLength(2)
  for (const l of lines) expect(l).toMatch(/^-\s*(我|对方)：/)
})

test('mechanicalDigest_两个角色的标签互不相同', () => {
  const labels = mechanicalDigest([turn('user', '甲'), turn('assistant', '乙')])
    .trim().split('\n').map(l => l.replace(/^-\s*/, '').split('：')[0])
  expect(new Set(labels).size).toBe(2)
})

test('mechanicalDigest_每轮按perTurnChars截断_尾部内容不出现', () => {
  const long = 'A'.repeat(5) + 'TAILMARK'
  expect(mechanicalDigest([turn('user', long)], { perTurnChars: 5 })).not.toContain('TAILMARK')
})

test('mechanicalDigest_默认每轮80字_第81字起被截掉', () => {
  expect(mechanicalDigest([turn('user', 'A'.repeat(80) + 'TAILMARK')])).not.toContain('TAILMARK')
})

test('mechanicalDigest_maxTurns限制行数_只留最近三条', () => {
  const turns = Array.from({ length: 20 }, (_, i) => turn('user', `第${i}句`))
  expect(mechanicalDigest(turns, { maxTurns: 3 }).trim().split('\n')).toHaveLength(3)
})

test('mechanicalDigest_降级也没内容_空数组返回空串', () => {
  expect(mechanicalDigest([])).toBe('')
})

test('mechanicalDigest_所有轮次都是空文本_返回空串', () => {
  expect(mechanicalDigest([turn('user', ''), turn('assistant', '   ')])).toBe('')
})

test('mechanicalDigest_内容含假令牌_输出已脱敏', () => {
  const out = mechanicalDigest([turn('user', `我的令牌 ${TG}`)])
  expect(out).not.toContain(TG)
  expect(out).toContain('[redacted]')
})

test('mechanicalDigest_传入null_返回空串不抛', () => {
  expect(mechanicalDigest(null as any)).toBe('')
})
