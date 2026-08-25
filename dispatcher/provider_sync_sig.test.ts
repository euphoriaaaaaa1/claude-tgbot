// 黑盒验收测试 · providerSig —— 唯一依据 .devflow/INTERFACE-provider-sync.md
// 实现尚不存在，跑红属预期。夹具全自造，绝不读真实 ~/.claude/settings.json。
import { test, expect } from 'bun:test'
import { providerSig } from './provider_sync'

const FAKE_TOKEN = 'sk-FAKE-abc123'
const FAKE_KEY = 'sk-FAKE-key789'
const FAKE_URL = 'https://api.fake-provider.test/anthropic'
const BASE_ENV = {
  ANTHROPIC_BASE_URL: FAKE_URL,
  ANTHROPIC_AUTH_TOKEN: FAKE_TOKEN,
  ANTHROPIC_API_KEY: FAKE_KEY,
  ANTHROPIC_MODEL: 'fake-model-x1',
}
// mk(env 覆盖, 顶层覆盖) → 一份假 settings.json 文本
const mk = (env: Record<string, unknown> = {}, top: Record<string, unknown> = {}) =>
  JSON.stringify({
    env: { ...BASE_ENV, ...env },
    model: 'fake-top-model',
    hooks: { PreToolUse: [] },
    statusLine: { type: 'command', command: 'echo hi' },
    ...top,
  })

// ---- 错误契约：入参非字符串 → TypeError ----
const NOT_STRINGS: [string, unknown][] = [
  ['null', null], ['undefined', undefined], ['数字', 42],
  ['对象', { env: {} }], ['数组', ['{}']], ['布尔', true],
]
for (const [label, v] of NOT_STRINGS) {
  test(`providerSig_入参是${label}_抛TypeError`, () => {
    expect(() => providerSig(v as any)).toThrow(TypeError)
  })
}

// ---- 读不出配置 → 空签名 '' ----
const EMPTY_SIG: [string, string][] = [
  ['空串', ''], ['纯空格', '   '], ['空白加换行', ' \n\t '],
  ['非法JSON', '{oops'], ['截断JSON', '{"env":'],
  ['JSON数组', '[]'], ['数组里套配置', '[{"env":{"ANTHROPIC_MODEL":"m"}}]'],
  ['JSON的null', 'null'], ['JSON数字', '42'], ['JSON字符串', '"text"'], ['JSON布尔', 'true'],
]
for (const [label, text] of EMPTY_SIG) {
  test(`providerSig_${label}_返回空签名`, () => {
    expect(providerSig(text)).toBe('')
  })
}

test('providerSig_非法JSON_只返回空不抛异常', () => {
  expect(() => providerSig('{oops')).not.toThrow()
})

// ---- 五个 provider 字段：逐个单独变化 → 签名必变 ----
const CHANGED: [string, string][] = [
  ['env.ANTHROPIC_BASE_URL', mk({ ANTHROPIC_BASE_URL: 'https://api.other-fake.test/anthropic' })],
  ['env.ANTHROPIC_AUTH_TOKEN', mk({ ANTHROPIC_AUTH_TOKEN: 'sk-FAKE-zzz999' })],
  ['env.ANTHROPIC_API_KEY', mk({ ANTHROPIC_API_KEY: 'sk-FAKE-key000' })],
  ['env.ANTHROPIC_MODEL', mk({ ANTHROPIC_MODEL: 'fake-model-x2' })],
  ['顶层 model', mk({}, { model: 'fake-top-model-2' })],
]
for (const [field, json] of CHANGED) {
  test(`providerSig_只改${field}_签名必变`, () => {
    expect(providerSig(json)).not.toBe(providerSig(mk()))
  })
}

test('providerSig_删掉一个provider字段_签名必变', () => {
  const { ANTHROPIC_API_KEY, ...rest } = BASE_ENV
  const withoutKey = JSON.stringify({ env: rest, model: 'fake-top-model' })
  const withKey = JSON.stringify({ env: BASE_ENV, model: 'fake-top-model' })
  expect(providerSig(withoutKey)).not.toBe(providerSig(withKey))
})

// ---- 反向用例：无关字段变化 → 签名不变 ----
const IRRELEVANT: [string, string][] = [
  ['hooks', mk({}, { hooks: { PreToolUse: [{ matcher: 'Bash' }] } })],
  ['statusLine', mk({}, { statusLine: { type: 'command', command: 'echo changed' } })],
  ['env 里的无关变量', mk({ SOME_OTHER_VAR: 'whatever' })],
  ['新增顶层无关键', mk({}, { permissions: { allow: ['Read'] }, theme: 'dark' })],
]
for (const [field, json] of IRRELEVANT) {
  test(`providerSig_只改${field}_签名不变`, () => {
    expect(providerSig(json)).toBe(providerSig(mk()))
  })
}

// ---- 确定性 ----
test('providerSig_同一输入调两次_结果完全相同', () => {
  expect(providerSig(mk())).toBe(providerSig(mk()))
})

test('providerSig_正常配置_签名非空', () => {
  expect(providerSig(mk())).not.toBe('')
})

// ---- 「配置存在但全空」必须区别于「读不出配置」 ----
test('providerSig_空对象配置_签名非空以区别于读不出配置', () => {
  expect(providerSig('{}')).not.toBe('')
})

test('providerSig_缺env块_签名与空串输入可区分', () => {
  expect(providerSig('{}')).not.toBe(providerSig(''))
})

test('providerSig_缺env块与空env块_签名相同', () => {
  expect(providerSig('{}')).toBe(providerSig('{"env":{}}'))
})

test('providerSig_五字段显式空串与字段缺失_签名相同', () => {
  const allEmpty = JSON.stringify({
    env: { ANTHROPIC_BASE_URL: '', ANTHROPIC_AUTH_TOKEN: '', ANTHROPIC_API_KEY: '', ANTHROPIC_MODEL: '' },
    model: '',
  })
  expect(providerSig(allEmpty)).toBe(providerSig('{}'))
})

// ---- 反向用例：签名里 grep 不到敏感明文（S6）----
test('providerSig_签名不含token和key明文', () => {
  const sig = providerSig(mk())
  expect(sig.includes(FAKE_TOKEN)).toBe(false)
  expect(sig.includes(FAKE_KEY)).toBe(false)
  expect(sig.includes('abc123')).toBe(false)
  expect(sig.includes('key789')).toBe(false)
})

// 契约主句「签名中不得包含任何字段明文」的严格解读；若实现只摘要敏感字段，此条会红，见 TEST-PLAN 歧义节
test('providerSig_签名不含baseURL与model明文', () => {
  const sig = providerSig(mk())
  expect(sig.includes(FAKE_URL)).toBe(false)
  expect(sig.includes('fake-model-x1')).toBe(false)
  expect(sig.includes('fake-top-model')).toBe(false)
})

// ---- 边界：Unicode / 超长值 ----
test('providerSig_字段含Unicode与emoji_签名确定且非空', () => {
  const json = mk({ ANTHROPIC_MODEL: '模型-テスト-🚀' })
  expect(providerSig(json)).toBe(providerSig(json))
  expect(providerSig(json)).not.toBe('')
})

test('providerSig_超长token_签名仍非空且与短token不同', () => {
  const long = 'sk-FAKE-' + 'x'.repeat(10000)
  expect(providerSig(mk({ ANTHROPIC_AUTH_TOKEN: long }))).not.toBe('')
  expect(providerSig(mk({ ANTHROPIC_AUTH_TOKEN: long }))).not.toBe(providerSig(mk()))
})

test('providerSig_字段值只差一个字符_签名必变', () => {
  expect(providerSig(mk({ ANTHROPIC_MODEL: 'fake-model-x1 ' })))
    .not.toBe(providerSig(mk({ ANTHROPIC_MODEL: 'fake-model-x1' })))
})
