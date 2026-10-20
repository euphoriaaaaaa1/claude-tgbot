// 白盒单测：send_error.ts（Bug2 分类器内部逻辑与边界）。
// 自私有版 dispatcher（~/.claude/dispatcher）移植。
// 与黑盒验收 tests/acceptance/send_error.test.ts 互补——这里专打验收没覆盖的边界：
// error_code 非典型值、retry_after 非 number、HttpError 内层为基本类型、instanceof 分流。
import { test, expect } from 'bun:test'
import { GrammyError, HttpError } from 'grammy'
import { isPreConnectFailure, classifySendError, describeSendError } from '../../send_error'

const grammyErr = (code: unknown, parameters: Record<string, unknown> = {}) =>
  new GrammyError('m', { ok: false, error_code: code, description: 'd', parameters } as never, 'sendMessage', {})
const httpErr = (error: unknown) => new HttpError('headers (network)', error)

// ─── isPreConnectFailure 集合边界 ─────────────────────────────────────

test('isPreConnectFailure_集合内每个码都true', () => {
  for (const c of ['ConnectionRefused', 'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'ENETUNREACH', 'EHOSTUNREACH']) {
    expect(isPreConnectFailure(c)).toBe(true)
  }
})

test('isPreConnectFailure_非string类型一律false', () => {
  for (const v of [0, 1, true, false, {}, [], NaN]) {
    expect(isPreConnectFailure(v as never)).toBe(false)
  }
})

test('isPreConnectFailure_近似但不等的码_返回false', () => {
  for (const c of ['ECONNREFUSED ', ' ECONNREFUSED', 'ECONNREFUSEDX', 'ECONNREFUSE', 'ENOTFOUND_', 'eai_again']) {
    expect(isPreConnectFailure(c)).toBe(false)
  }
})

// ─── classifySendError：GrammyError 边界 ──────────────────────────────

test('classifySendError_GrammyError_399归ambiguous', () => {
  const r = classifySendError(grammyErr(399))
  expect(r.cls).toBe('ambiguous')
  expect(r.reason).toBe('GrammyError 399')
})

test('classifySendError_GrammyError_499归undelivered_500归ambiguous_分界', () => {
  expect(classifySendError(grammyErr(499)).cls).toBe('undelivered')
  expect(classifySendError(grammyErr(500)).cls).toBe('ambiguous')
})

test('classifySendError_GrammyError_200归ambiguous', () => {
  expect(classifySendError(grammyErr(200)).cls).toBe('ambiguous')
})

test('classifySendError_GrammyError429_retry_after非number_不设retryAfterSec', () => {
  const r = classifySendError(grammyErr(429, { retry_after: '3' }))
  expect(r.cls).toBe('retryable')
  expect(r.retryAfterSec).toBeUndefined()
})

test('classifySendError_GrammyError429_retry_after为0_保留0', () => {
  const r = classifySendError(grammyErr(429, { retry_after: 0 }))
  expect(r.retryAfterSec).toBe(0)
})

test('classifySendError_GrammyError_非429无retryAfterSec键', () => {
  expect('retryAfterSec' in classifySendError(grammyErr(400))).toBe(false)
})

// ─── classifySendError：HttpError 边界 ────────────────────────────────

test('classifySendError_HttpError_error为null_归ambiguous_reason=HttpError', () => {
  const r = classifySendError(httpErr(null))
  expect(r).toEqual({ cls: 'ambiguous', reason: 'HttpError' })
})

test('classifySendError_HttpError_error为字符串_不抛且reason=HttpError', () => {
  const r = classifySendError(httpErr('boom'))
  expect(r.cls).toBe('ambiguous')
  expect(r.reason).toBe('HttpError')
})

test('classifySendError_HttpError_cause为字符串_取其下的code失败_退回name', () => {
  // cause 非对象 → cause.code 取不到；name 也无 → reason=HttpError。
  const r = classifySendError(httpErr({ cause: 'not-an-object' }))
  expect(r.reason).toBe('HttpError')
})

test('classifySendError_HttpError_code与cause都在_code优先', () => {
  const r = classifySendError(httpErr({ code: 'ENETUNREACH', cause: { code: 'ECONNRESET' } }))
  expect(r.cls).toBe('retryable')
  expect(r.reason).toBe('code=ENETUNREACH')
})

test('classifySendError_HttpError_instanceof不误判为GrammyError', () => {
  const r = classifySendError(httpErr({ code: 'ConnectionRefused' }))
  expect(r.reason).toBe('code=ConnectionRefused')   // 若被当 GrammyError，reason 会是 'GrammyError ...'
})

test('classifySendError_GrammyError_instanceof不误判为HttpError', () => {
  const r = classifySendError(grammyErr(429, { retry_after: 3 }))
  expect(r.reason).toBe('GrammyError 429')
  expect(r.retryAfterSec).toBe(3)
})

// ─── classifySendError：其它输入 ──────────────────────────────────────

test('classifySendError_无原型对象_归unknown', () => {
  expect(classifySendError(Object.create(null))).toEqual({ cls: 'ambiguous', reason: 'unknown' })
})

test('classifySendError_不是Error的类Error对象_归unknown', () => {
  // 带 error_code/error 字段但不是真实实例 → 仍归 unknown（instanceof 分流）。
  expect(classifySendError({ error_code: 429, error: { code: 'ECONNREFUSED' } })).toEqual({ cls: 'ambiguous', reason: 'unknown' })
})

test('classifySendError_Promise与Symbol_归unknown不抛', () => {
  expect(classifySendError(Promise.resolve(1))).toEqual({ cls: 'ambiguous', reason: 'unknown' })
  expect(classifySendError(Symbol('x'))).toEqual({ cls: 'ambiguous', reason: 'unknown' })
})

// ─── describeSendError 边界 ───────────────────────────────────────────

test('describeSendError_retryAfterSec为null_不追加', () => {
  expect(describeSendError({ cls: 'retryable', reason: 'r', retryAfterSec: undefined })).toBe('cls=retryable reason=r')
})

test('describeSendError_retryAfterSec为负数_仍追加', () => {
  expect(describeSendError({ cls: 'retryable', reason: 'r', retryAfterSec: -1 })).toBe('cls=retryable reason=r retry_after=-1s')
})

test('describeSendError_入参undefined_不抛', () => {
  expect(() => describeSendError(undefined as never)).not.toThrow()
})

// ─── 隐私：reason 不含底层 message / cause 内容 ───────────────────────

test('隐私_HttpError_cause带敏感串_reason只含code', () => {
  const r = classifySendError(httpErr({ cause: { code: 'ENOTFOUND', message: 'SECRET_CAUSE_MSG' } }))
  expect(r.reason).toBe('code=ENOTFOUND')
  expect(describeSendError(r)).not.toContain('SECRET_CAUSE_MSG')
})

test('隐私_classifySendError_永不抛_遍历怪输入', () => {
  const weird = [undefined, null, 0, '', 'x', 1n, {}, [], () => {}, new Map(), new Date(), /re/, Object.create(null)]
  for (const v of weird) expect(() => classifySendError(v)).not.toThrow()
})
