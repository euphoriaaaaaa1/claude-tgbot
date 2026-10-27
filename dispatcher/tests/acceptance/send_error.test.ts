// 黑盒验收：send_error.ts（Bug2 —— 发送失败即丢消息）
// 自私有版 dispatcher（~/.claude/dispatcher）移植。分类字面量依据私有版
// INTERFACE-providerfp-msgretry.md §2.4 的锁死表。
// 只通过 isPreConnectFailure / classifySendError / describeSendError 调用；用真实 grammy 类构造实例。
import { test, expect } from 'bun:test'
import { GrammyError, HttpError } from 'grammy'
import { isPreConnectFailure, classifySendError, describeSendError } from '../../send_error'

/** 造真实 GrammyError；retryAfter 省略则 parameters 缺省（空对象） */
const grammyErr = (code: number, opts: { retryAfter?: number } = {}) =>
  new GrammyError(
    'Call to sendMessage failed!',
    {
      ok: false,
      error_code: code,
      description: 'test description',
      ...(opts.retryAfter !== undefined ? { parameters: { retry_after: opts.retryAfter } } : {}),
    } as never,
    'sendMessage',
    {},
  )

/** 造真实 HttpError，内层 error 即 fetch 抛出的对象（可带 code / cause.code / name） */
const httpErr = (error: unknown) => new HttpError('Network request failed', error)

// ---------------------------------------------------------------------------
// isPreConnectFailure：精确字符串匹配，"建连前失败"集合内外
// ---------------------------------------------------------------------------

for (const code of ['ConnectionRefused', 'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'ENETUNREACH', 'EHOSTUNREACH']) {
  test(`isPreConnectFailure_已确证建连前集合_${code}_返回true`, () => {
    expect(isPreConnectFailure(code)).toBe(true)
  })
}

for (const code of ['ECONNRESET', 'ETIMEDOUT', 'ECONNABORTED', 'AbortError', 'UND_ERR_CONNECT_TIMEOUT']) {
  test(`isPreConnectFailure_不属建连前_${code}_返回false`, () => {
    expect(isPreConnectFailure(code)).toBe(false)
  })
}

test('isPreConnectFailure_空串_返回false', () => {
  expect(isPreConnectFailure('')).toBe(false)
})

test('isPreConnectFailure_大小写不折叠_小写econnrefused_返回false', () => {
  expect(isPreConnectFailure('econnrefused')).toBe(false)
})

test('isPreConnectFailure_非子串匹配_带前后缀_返回false', () => {
  expect(isPreConnectFailure('ECONNREFUSED ')).toBe(false)
  expect(isPreConnectFailure('xECONNREFUSED')).toBe(false)
  expect(isPreConnectFailure('ECONNREFUSED_EXTRA')).toBe(false)
})

test('isPreConnectFailure_永不抛_怪输入一律false', () => {
  for (const v of [null, undefined, 0, {}, [], Symbol('x'), 'a'.repeat(5000), '中文错误码']) {
    expect(isPreConnectFailure(v as unknown as string)).toBe(false)
  }
})

// ---------------------------------------------------------------------------
// classifySendError：按 §2.4 字面量锁死表逐行核对
// ---------------------------------------------------------------------------

test('classifySendError_GrammyError429_带retry_after_归retryable', () => {
  const r = classifySendError(grammyErr(429, { retryAfter: 3 }))
  expect(r).toEqual({ cls: 'retryable', reason: 'GrammyError 429', retryAfterSec: 3 })
})

test('classifySendError_GrammyError429_无retry_after_归retryable且retryAfterSec为undefined', () => {
  const r = classifySendError(grammyErr(429))
  expect(r.cls).toBe('retryable')
  expect(r.reason).toBe('GrammyError 429')
  expect(r.retryAfterSec).toBeUndefined()
})

test('classifySendError_GrammyError400_归undelivered', () => {
  const r = classifySendError(grammyErr(400))
  expect(r.cls).toBe('undelivered')
  expect(r.reason).toBe('GrammyError 400')
})

for (const code of [403, 404, 413]) {
  test(`classifySendError_GrammyError${code}_归undelivered`, () => {
    const r = classifySendError(grammyErr(code))
    expect(r.cls).toBe('undelivered')
    expect(r.reason).toBe(`GrammyError ${code}`)
  })
}

for (const code of [401, 499]) {
  test(`classifySendError_GrammyError${code}_落在4xx区间_归undelivered`, () => {
    expect(classifySendError(grammyErr(code)).cls).toBe('undelivered')
  })
}

for (const code of [500, 502, 503]) {
  test(`classifySendError_GrammyError${code}_归ambiguous`, () => {
    const r = classifySendError(grammyErr(code))
    expect(r.cls).toBe('ambiguous')
    expect(r.reason).toBe(`GrammyError ${code}`)
  })
}

test('classifySendError_非429_不携带retryAfterSec', () => {
  expect(classifySendError(grammyErr(400)).retryAfterSec).toBeUndefined()
  expect(classifySendError(httpErr({ code: 'ECONNREFUSED' })).retryAfterSec).toBeUndefined()
})

test('classifySendError_HttpError_bun码ConnectionRefused_归retryable', () => {
  const r = classifySendError(httpErr({ code: 'ConnectionRefused' }))
  expect(r).toEqual({ cls: 'retryable', reason: 'code=ConnectionRefused' })
})

test('classifySendError_HttpError_Node码ECONNREFUSED_归retryable', () => {
  const r = classifySendError(httpErr({ code: 'ECONNREFUSED' }))
  expect(r).toEqual({ cls: 'retryable', reason: 'code=ECONNREFUSED' })
})

test('classifySendError_HttpError_code在cause上ENOTFOUND_归retryable', () => {
  const r = classifySendError(httpErr({ cause: { code: 'ENOTFOUND' } }))
  expect(r.cls).toBe('retryable')
  expect(r.reason).toBe('code=ENOTFOUND')
})

test('classifySendError_HttpError_code优先于cause_code', () => {
  const r = classifySendError(httpErr({ code: 'ECONNREFUSED', cause: { code: 'ECONNRESET' } }))
  expect(r).toEqual({ cls: 'retryable', reason: 'code=ECONNREFUSED' })
})

// --- Bug2 核心反向：可能已送达的错误绝不归 retryable ---

for (const code of ['ECONNRESET', 'ETIMEDOUT', 'ECONNABORTED', 'UND_ERR_CONNECT_TIMEOUT']) {
  test(`Bug2核心_HttpError_${code}_归ambiguous不重试`, () => {
    const r = classifySendError(httpErr({ code }))
    expect(r.cls).toBe('ambiguous')
    expect(r.reason).toBe(`code=${code}`)
  })
}

test('Bug2核心_HttpError_name为AbortError_归ambiguous', () => {
  const r = classifySendError(httpErr({ name: 'AbortError' }))
  expect(r.cls).toBe('ambiguous')
  expect(r.reason).toBe('code=AbortError')
})

test('classifySendError_HttpError_无code无name_归ambiguous且reason为HttpError', () => {
  const r = classifySendError(httpErr({}))
  expect(r).toEqual({ cls: 'ambiguous', reason: 'HttpError' })
})

test('classifySendError_普通Error_归ambiguous且reason为unknown', () => {
  const r = classifySendError(new Error('plain failure'))
  expect(r.cls).toBe('ambiguous')
  expect(r.reason).toBe('unknown')
})

test('classifySendError_字符串null数字_一律ambiguous_unknown', () => {
  expect(classifySendError('boom')).toEqual({ cls: 'ambiguous', reason: 'unknown' })
  expect(classifySendError(null)).toEqual({ cls: 'ambiguous', reason: 'unknown' })
  expect(classifySendError(42)).toEqual({ cls: 'ambiguous', reason: 'unknown' })
})

test('classifySendError_永不抛_各色怪输入', () => {
  for (const v of [undefined, null, 0, '', 'boom', {}, [], Symbol('s'), () => {}, new Error('plain')]) {
    expect(() => classifySendError(v)).not.toThrow()
  }
})

test('classifySendError_幂等_同一错误两次分类相同', () => {
  const e = grammyErr(429, { retryAfter: 2 })
  expect(classifySendError(e)).toEqual(classifySendError(e))
})

// --- 隐私反向：reason 绝不携带 String(e) 全文 / token / 底层 message ---

test('隐私_classifySendError_reason不含GrammyError原文', () => {
  const leak = 'SECRET_TOKEN_ZZZ9'
  const e = new GrammyError(leak, { ok: false, error_code: 429, description: 'd', parameters: {} } as never, 'sendMessage', {})
  const r = classifySendError(e)
  expect(r.reason).toBe('GrammyError 429')
  expect(r.reason).not.toContain(leak)
  expect(describeSendError(r)).not.toContain(leak)
})

test('隐私_classifySendError_HttpError_reason不含底层message', () => {
  const leak = 'SECRET_UNDERLYING_YYY'
  const r = classifySendError(httpErr({ message: leak }))
  expect(r.reason).toBe('HttpError')
  expect(r.reason).not.toContain(leak)
})

// ---------------------------------------------------------------------------
// describeSendError：cls=<cls> reason=<reason> [+ retry_after=<n>s]
// ---------------------------------------------------------------------------

test('describeSendError_429带retry_after_逐字一致', () => {
  expect(describeSendError({ cls: 'retryable', reason: 'GrammyError 429', retryAfterSec: 3 }))
    .toBe('cls=retryable reason=GrammyError 429 retry_after=3s')
})

test('describeSendError_429无retry_after_不追加段', () => {
  expect(describeSendError({ cls: 'retryable', reason: 'GrammyError 429' }))
    .toBe('cls=retryable reason=GrammyError 429')
})

test('describeSendError_undelivered_逐字一致', () => {
  expect(describeSendError({ cls: 'undelivered', reason: 'GrammyError 400' }))
    .toBe('cls=undelivered reason=GrammyError 400')
})

test('describeSendError_ambiguous_逐字一致', () => {
  expect(describeSendError({ cls: 'ambiguous', reason: 'HttpError' }))
    .toBe('cls=ambiguous reason=HttpError')
})

test('describeSendError_retry_after为0_仍追加0s', () => {
  expect(describeSendError({ cls: 'retryable', reason: 'GrammyError 429', retryAfterSec: 0 }))
    .toBe('cls=retryable reason=GrammyError 429 retry_after=0s')
})

test('describeSendError_永不抛_含怪输入', () => {
  expect(() => describeSendError({ cls: 'ambiguous', reason: 'unknown' })).not.toThrow()
  expect(() => describeSendError({ cls: 'nope' as never, reason: '' })).not.toThrow()
})

test('describeSendError_幂等_同输入两次相同', () => {
  const info = { cls: 'retryable' as const, reason: 'GrammyError 429', retryAfterSec: 1 }
  expect(describeSendError(info)).toBe(describeSendError(info))
})

test('联动_classify转describe_429带retry_after_输出锁定表结果', () => {
  expect(describeSendError(classifySendError(grammyErr(429, { retryAfter: 3 }))))
    .toBe('cls=retryable reason=GrammyError 429 retry_after=3s')
})

test('联动_classify转describe_HttpError建连失败', () => {
  expect(describeSendError(classifySendError(httpErr({ code: 'ConnectionRefused' }))))
    .toBe('cls=retryable reason=code=ConnectionRefused')
})
