// H2b 在飞消息重放 —— 黑盒验收(bug 修复模式:这些用例当前应为红,修好后转绿)
// 只依据 .devflow/{BRIEF,INTERFACE}-h2b-replay.md。不读实现。
// 全程假 worker/假时钟/假发送/临时目录,绝不真起进程、不发真消息、不碰真实信道目录。
// 跑法:bun test tests/acceptance/h2b_replay/
import { test, expect, beforeEach, afterEach, afterAll } from 'bun:test'
import { boot, captureStderr, releaseStderr, eventText, isPendingName, cleanupAll, type Harness } from './_fixture.ts'

const HARNS: Harness[] = []
beforeEach(() => captureStderr())
afterEach(() => { releaseStderr(); HARNS.splice(0) })
afterAll(() => cleanupAll())
async function H(): Promise<Harness> { const h = await boot(); HARNS.push(h); return h }

const ev = () => eventText()
const has = (s: string) => ev().includes(s)
const curAttempt = (h: Harness): number => {
  const n = h.inflight()[0] ?? ''
  const m = n.match(/\.inflight-(\d+)-/)
  return m ? Number(m[1]) : NaN
}
const queueStr = (h: Harness): string => h.m.queue.map((q: any) => q.path ?? q.content ?? JSON.stringify(q)).join('|')

// ═══ 契约 §一.1 投递改名 ════════════════════════════════════════════════════
test('§1 投递后:原名消失、.inflight-* 存在、attempt 从 0 起、不以 .json 结尾', async () => {
  const h = await H()
  const p = h.put('1700000000000_1.json')
  await h.deliver(p)
  const infl = h.inflight()
  expect(h.exists(h.pendingDir, '1700000000000_1.json')).toBe(false) // 原名不在
  expect(infl.length).toBe(1)                                        // 恰一个在飞件
  expect(infl[0]).toMatch(/^\.inflight-0-1700000000000_1\.part$/)    // attempt=0,同 stem,后缀 .part
  expect(infl[0].endsWith('.json')).toBe(false)                      // 契约:新名不以 .json 结尾
})

test('§1 在飞件名不被既有"待投件"扫描当作新消息(假扫描函数)', async () => {
  const h = await H()
  const orig = '1700000000001_2.json'
  expect(isPendingName(orig)).toBe(true)          // 反向锚点:原名确实算待投件
  await h.deliver(h.put(orig))
  expect(h.inflight().length).toBe(1)             // 确有一个在飞件(否则下面反向断言为空转)
  const pendingSeen = h.namesIn(h.pendingDir).filter(isPendingName)
  expect(pendingSeen).not.toContain(h.inflight()[0])   // 在飞件不进待投集合
})

// ═══ 契约 §六.12 一件同一时刻只在一个名字下 ═════════════════════════════════
test('§12 投递前后:同一件不同时以原名与 .inflight-* 出现在 .pending 里', async () => {
  const h = await H()
  const name = '1700000000002_3.json'
  const p = h.put(name)
  h.enqueue(p)                                    // 只入队,未投递
  expect(h.exists(h.pendingDir, name)).toBe(true)
  expect(h.inflight().length).toBe(0)             // 投递前无在飞件
  await h.deliver(p)                              // 投递
  expect(h.exists(h.pendingDir, name)).toBe(false)
  expect(h.inflight().length).toBe(1)             // 任一时刻只有一个名字
})

// ═══ 契约 §一.2 成功结果删除在飞件 ══════════════════════════════════════════
test('§2 attempt 一致的成功 result(is_error 非 true):删除该在飞件', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000003_4.json'))
  expect(h.inflight().length).toBe(1)
  h.result({ type: 'result', attempt: 0, is_error: false })
  expect(h.inflight().length).toBe(0)             // 该轮成功 → 在飞件被删
})

// ═══ 契约 §一.2b 迟到事件不删不动 ═══════════════════════════════════════════
test('§2b 迟到 result(attempt 不一致):只记 wm_stale_result,文件仍在、状态不变', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000004_5.json'))
  const phase0 = h.phase()
  h.result({ type: 'result', attempt: 1, is_error: false })   // 非当前在飞 attempt
  expect(has('wm_stale_result')).toBe(true)
  expect(h.inflight().length).toBe(1)             // 文件仍在
  expect(h.phase()).toBe(phase0)                  // 状态不变
})

test('§2b 迟到 exit(attempt 不一致):只记 wm_stale_exit,文件仍在、phase 不变', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000005_6.json'))
  const phase0 = h.phase()
  h.exit(0, { attempt: 1 })                       // 非当前在飞 attempt 的退出
  expect(has('wm_stale_exit')).toBe(true)
  expect(h.inflight().length).toBe(1)
  expect(h.phase()).toBe(phase0)
})

// ═══ 契约 §一.3 启动扫描:在飞件与待投件都入队,按时间前缀排序 ═══════════════
test('§3 启动扫描:.inflight-* 与其余待投件都入队,且按时间前缀排序(非目录枚举序)', async () => {
  const h = await H()
  // 写入序≠时间序:故意让目录枚举序与时间前缀相反
  h.putRaw('.inflight-1-1700000001000_2.part', JSON.stringify({ chat_id: 'FAKECHAT-1', text: 'INFLIGHT' }))
  h.put('1700000003000_9.json', { text: 'NEWER' })
  h.put('1700000002000_5.json', { text: 'MIDDLE' })
  h.m.queue = []
  h.m.drainInbox()
  const q = queueStr(h)
  expect(q).toContain('1700000001000')
  expect(q).toContain('1700000002000')
  expect(q).toContain('1700000003000')
  expect(q.indexOf('1700000001000')).toBeLessThan(q.indexOf('1700000002000'))
  expect(q.indexOf('1700000002000')).toBeLessThan(q.indexOf('1700000003000'))
})

// ═══ 契约 §一.4 + §四.10 可重试 → 重放,n 加 1 ═══════════════════════════════
test('§4 可重试错误触发重放一次:文件名里 n 加 1', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000010_7.json'))
  expect(curAttempt(h)).toBe(0)
  h.result({ type: 'result', is_error: true, status: 500, attempt: 0 })   // 5xx → retryable
  expect(has('wm_replay')).toBe(true)
  expect(curAttempt(h)).toBe(1)                            // 0 → 1
  expect(h.inflight()[0]).toMatch(/\.part$/)
})

// ═══ 契约 §二.5 超上限 → 死信 + stderr + 一次用户提示 ═══════════════════════
function driveToDeadLetter(h: Harness) {
  for (let i = 0; i < 8; i++) {
    const infl = h.inflight()
    if (infl.length === 0) break
    const a = Number((infl[0].match(/\.inflight-(\d+)-/) ?? [])[1])
    h.result({ type: 'result', is_error: true, status: 500, attempt: a })   // 持续可重试 → 直到超限
  }
}
test('§5 重放至上限:进 .dead-letter/、stderr 写 wm_dead_letter id=、发送一次非空提示', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000011_8.json'))
  driveToDeadLetter(h)
  const dead = h.namesIn(h.deadDir)
  expect(dead.length).toBe(1)                       // 落到死信目录
  expect(dead[0]).toContain('1700000000011')        // 是这一件
  expect(has('wm_dead_letter id=')).toBe(true)      // 事件行
  expect(h.sent.length).toBe(1)                     // 恰好一次用户提示
  expect(h.sent[0].text.length).toBeGreaterThan(0)  // 文案非空
  expect(h.inflight().length).toBe(0)               // 死信后不再有在飞件
})

// ═══ 契约 §二.6 死信后不再入队 ═══════════════════════════════════════════════
test('§6 死信后:后续 pump 不再取到该件,也不再重复提示', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000012_9.json'))
  driveToDeadLetter(h)
  const sentN = h.sent.length
  h.m.queue = []
  await h.m.pump(); await h.m.pump()                // 再泵几次
  expect(h.namesIn(h.deadDir).length).toBe(1)       // 死信件仍在死信目录
  expect(h.inflight().length).toBe(0)               // 不会被重新捡起投递
  expect(h.sent.length).toBe(sentN)                 // 不重复提示
})

// ═══ 契约 §三.7–8 超时口径:从最近一次 worker 事件起算 ═══════════════════════
const STALL = 5 * 60_000

test('§7 持续有 stdout 事件:每次都重置计时,始终不触发 wm_stall', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000020_1.json'))
  for (let i = 0; i < 3; i++) {
    h.stdout(`chunk-${i}\n`)            // 每段事件把计时清零
    h.advance(STALL - 1)                // 差 1ms 不到点
  }
  expect(has('wm_stall')).toBe(false)   // 累计已远超 STALL_MS,但因有事件而未触发
  expect(h.inflight().length).toBe(1)
})

test('§8 静默满 STALL_MS 无 result:触发 wm_stall attempts=<n>', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000021_2.json'))
  h.advance(STALL)                      // 期间无任何 worker 事件
  expect(has('wm_stall attempts=')).toBe(true)
})

// ═══ 契约 §四.9 is_error 分类(经 stderr 的 wm_result_error kind= 观察)════════
async function kindOf(extra: Record<string, unknown>): Promise<string> {
  const h = await H()
  await h.deliver(h.put(`1700000000099_${Math.floor(Math.random() * 900 + 100)}.json`))
  h.result({ type: 'result', is_error: true, attempt: curAttempt(h), ...extra })
  const m = ev().match(/wm_result_error kind=(\w+)/)
  return m ? m[1] : '(未写 wm_result_error)'
}
test('§9 结构化 401 → fatal', async () => {
  expect(await kindOf({ status: 401 })).toBe('fatal')
})
test('§9 文本 "HTTP 401"(有 HTTP 上下文)→ fatal', async () => {
  expect(await kindOf({ message: 'HTTP 401 Unauthorized', text: 'HTTP 401 Unauthorized' })).toBe('fatal')
})
test('§9 文本 "4041 ms"(无 HTTP 上下文)→ retryable', async () => {
  expect(await kindOf({ message: 'took 4041 ms', text: 'took 4041 ms' })).toBe('retryable')
})

// ═══ 契约 §四.10 两类错误都不得静默丢弃 ═════════════════════════════════════
test('§10 kind=retryable → 走重放(有 wm_replay),不静默丢弃', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000022_3.json'))
  h.result({ type: 'result', is_error: true, status: 503, attempt: 0 })
  expect(has('wm_replay')).toBe(true)
})

test('§10 kind=fatal → 走死信 + 用户提示(发送被调用一次),不静默丢弃', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000023_4.json'))
  h.result({ type: 'result', is_error: true, status: 401, attempt: 0 })   // 401 → fatal
  expect(h.namesIn(h.deadDir).length).toBe(1)             // 进死信
  expect(h.sent.length).toBe(1)                           // 有可见提示
  expect(has('wm_result_error kind=fatal')).toBe(true)
})

// ═══ 契约 §六.11 正常成功:与现状一致(清在飞件、放行下一件、无错误事件) ══════
test('§11 正常成功:清在飞件,无 stale/dead_letter,不发提示;下一件照常可投', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000024_5.json'))
  h.result({ type: 'result', is_error: false, attempt: 0 })
  expect(h.inflight().length).toBe(0)                     // 在飞件已清
  expect(has('wm_stale_result')).toBe(false)              // 无迟到事件
  expect(has('wm_dead_letter')).toBe(false)               // 无死信
  expect(h.sent.length).toBe(0)                           // 成功不打扰用户
  await h.deliver(h.put('1700000000025_6.json'))          // 下一件
  expect(curAttempt(h)).toBe(0)
  expect(h.inflight()[0]).toContain('1700000000025')
})

// ═══ 契约 §五.12b 停机期间不重放,下次启动扫描兜底入队 ═══════════════════════
test('§12b stopping 期间不发起重放:.inflight-* 保持、无 wm_replay;下次扫描入队', async () => {
  const h = await H()
  await h.deliver(h.put('1700000000026_7.json'))
  h.m.phaseVal = 'stopping'                               // 注入"停机进行中"
  h.result({ type: 'result', is_error: true, status: 503, attempt: curAttempt(h) })  // 可重试,但停机中
  expect(has('wm_replay')).toBe(false)                    // 不重放
  expect(h.inflight().length).toBe(1)                     // 在飞件保持
  // 下次启动扫描:兜底入队
  h.m.phaseVal = 'running'
  h.m.queue = []
  h.m.drainInbox()
  expect(queueStr(h)).toContain('1700000000026')
})
