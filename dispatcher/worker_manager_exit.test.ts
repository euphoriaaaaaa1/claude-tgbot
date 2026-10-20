// 白盒测试 · WorkerManager —— onExit（陈旧退出守卫 + 在飞消息回插）与 inbox→队列的波次层。
// 全程假 proc、临时 CHANNEL_DIR，不起真 claude、不碰真实 ~/.claude。
//
// ⚠️ WorkerManager 的白盒测试请一律加在本文件里，别另起一个 *.test.ts：bun test 全仓共用
// 一份模块缓存，worker-manager 的模块级 CHANNEL_DIR 由**第一个** import 它的测试文件定死，
// 第二个文件再设 env 也没用（会拿到别人的临时目录，按文件名顺序随机翻车）。
import { test, expect, afterEach } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'
import { createBurstCollector } from './burst_inbox'
import { winCmdLine } from './win_cmd'

// 模块级 CHANNEL_DIR 在 import 时就定死，必须先设 env 再动态 import
const TMP = mkdtempSync(join(tmpdir(), 'worker-exit-test-'))
process.env.CHANNEL_DIR = TMP
process.env.BOT_NAME = 'testbot'
const { WorkerManager, cliVersion, stripThinking, compactSessionIfHuge, classifyResultError, parseInflightName } = await import('./worker-manager')

afterEach(() => { rmSync(TMP, { recursive: true, force: true }) })

// 绕开构造函数：它会装 fs.watch + 两个常驻 setInterval，泄进测试进程里没人收得掉。
// 这里只给 onExit 真正读写的那几个字段，spawnWorker 用桩顶掉（否则 1 秒后真去起 claude）。
type FakeProc = { pid: number }
function mkManager(init: Record<string, unknown> = {}) {
  const m = Object.create(WorkerManager.prototype) as any
  Object.assign(m, {
    proc: null, phaseVal: 'stopped', handledExit: false, pendingRestart: false, stopRequested: false,
    pid: null, stopWaitResolve: null, stopGraceTimer: null, stopTimeoutTimer: null,
    inFlight: null, inFlightContent: '', inFlightSince: 0,
    queue: [], restartAttempt: 0, intentionalKill: false, resultTimer: null,
    stdoutBuf: '', watchers: [], needsReplyReminder: false,
    channelDir: TMP, now: () => Date.now(),
    timer: { setTimeout, clearTimeout, setInterval, clearInterval },
    ...init,
  })
  m.spawnCalls = 0
  m.spawnWorker = async () => { m.spawnCalls++ }
  m.drainInbox = () => {}
  return m
}
const fakeProc = (pid: number) => ({ pid } as FakeProc)
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

// ─── 陈旧退出守卫 ───────────────────────────────────────────────────────
test('陈旧proc的exit_不清掉新worker状态_也不多排一次spawn', async () => {
  const oldP = fakeProc(111), newP = fakeProc(222)
  // kill(oldP) 之后、exit 事件到达之前，有消息进来 ensure() 起了 newP
  const m = mkManager({ proc: newP, phaseVal: 'running' })
  m.onExit(0, oldP)
  expect(m.proc).toBe(newP)          // 新 worker 没被清掉
  expect(m.phase()).toBe('running')
  await sleep(1200)                  // BACKOFF_MS[0] = 1000
  expect(m.spawnCalls).toBe(0)       // 没有第二个 claude 被排上去
})

test('当前proc的exit_照常走重启', async () => {
  const p = fakeProc(111)
  const m = mkManager({ proc: p, phaseVal: 'running' })
  m.onExit(1, p)
  expect(m.proc).toBe(null)
  expect(m.phase()).toBe('stopped')
  await sleep(1200)
  expect(m.spawnCalls).toBe(1)
})

test('kill后proc已置空的exit_仍走重启', async () => {
  // kill() 把 proc 置 null 后 exit 才到：这是最常见的主动重启路径，不能被守卫拦掉
  const m = mkManager({ proc: null, phaseVal: 'stopped' })
  m.onExit(0, fakeProc(111))
  await sleep(1200)
  expect(m.spawnCalls).toBe(1)
})

test('不带proc参数的老调用_不受守卫影响', async () => {
  const m = mkManager({ proc: fakeProc(999), phaseVal: 'running' })
  m.onExit(0)
  expect(m.proc).toBe(null)
  await sleep(1200)
  expect(m.spawnCalls).toBe(1)
})

// ─── 在飞消息回插 ───────────────────────────────────────────────────────
test('在飞file消息_按raw回插到队头_内容一字不改', async () => {
  const p = fakeProc(1)
  const content = '【私聊】<channel chat_id="42">你好</channel>'
  const m = mkManager({
    proc: p, phaseVal: 'running',
    inFlight: { kind: 'file', path: '/tmp/fake-inbox/42_7.json' },  // 该文件写 stdin 时已删
    inFlightContent: content,
    queue: [{ kind: 'file', path: '/tmp/fake-inbox/42_8.json' }],
  })
  m.onExit(1, p)
  expect(m.queue.length).toBe(2)
  expect(m.queue[0]).toEqual({ kind: 'raw', content, label: 'requeue-after-restart' })
  expect(m.queue[1]).toEqual({ kind: 'file', path: '/tmp/fake-inbox/42_8.json' })  // 排在它后面
  expect(m.inFlight).toBe(null)
  expect(m.inFlightContent).toBe('')
  await sleep(1200)
  expect(m.spawnCalls).toBe(1)
})

test('在飞raw消息_原样回插不重新包装', () => {
  const p = fakeProc(1)
  const item = { kind: 'raw', content: '/compact', label: 'slash /compact' }
  const m = mkManager({ proc: p, phaseVal: 'running', inFlight: item, inFlightContent: '/compact' })
  m.onExit(1, p)
  expect(m.queue[0]).toBe(item)      // 同一个对象，label 保住（pump 靠它认 slash）
})

test('回插的label不以slash开头_不会被当成命令跳过冷轮提醒', () => {
  const p = fakeProc(1)
  const m = mkManager({
    proc: p, phaseVal: 'running',
    inFlight: { kind: 'file', path: '/tmp/x.json' }, inFlightContent: '正文',
  })
  m.onExit(1, p)
  expect((m.queue[0].label as string).startsWith('slash')).toBe(false)
})

test('在飞内容为空_不回插空消息', () => {
  const p = fakeProc(1)
  const m = mkManager({
    proc: p, phaseVal: 'running',
    inFlight: { kind: 'file', path: '/tmp/x.json' }, inFlightContent: '',
  })
  m.onExit(1, p)
  expect(m.queue.length).toBe(0)     // 空 content 回插只会给 claude 喂一条空消息
})

test('intentional kill_不回插不重启', async () => {
  const p = fakeProc(1)
  const m = mkManager({
    proc: p, phaseVal: 'running', intentionalKill: true,
    inFlight: { kind: 'file', path: '/tmp/x.json' }, inFlightContent: '正文',
  })
  m.onExit(0, p)
  expect(m.queue.length).toBe(0)     // /clearall 之类：本来就不该把旧消息带回来
  await sleep(1200)
  expect(m.spawnCalls).toBe(0)
})

test('没有在飞消息_队列不动', () => {
  const p = fakeProc(1)
  const m = mkManager({ proc: p, phaseVal: 'running', queue: [{ kind: 'file', path: '/tmp/y.json' }] })
  m.onExit(1, p)
  expect(m.queue.length).toBe(1)
})

// ─── inbox → 队列的波次层接线（凑一波再递）──────────────────────────────
// 纯逻辑与落盘由 burst.test.ts / burst_collect.test.ts 覆盖，这里只验两件接线的事：
// 空闲时首条不入队、以及 5s 兜底扫重复看见同一个原件时不会把它再入一次队。
function mkBurstManager(windowMs = 5000) {
  const inbox = join(TMP, 'inbox')
  const dest = join(TMP, '.burst')
  mkdirSync(inbox, { recursive: true })
  let clock = 1_000_000
  let seq = 0
  const m = mkManager({ phaseVal: 'running' })
  delete m.drainInbox          // mkManager 把它桩掉了，这几条测的就是它
  m.ensure = async () => {}
  m.pump = () => {}
  m.burst = createBurstCollector({
    windowMs, maxMs: 12_000, destDir: dest, now: () => clock,
    deliver: (p: string) => m.enqueueFile(p),
  })
  return {
    m,
    at: (ms: number) => { clock = ms },
    tick: () => m.burst.tick(),
    add: (text: string, extra: Record<string, unknown> = {}) => {
      const p = join(inbox, `FAKECHAT-1_${++seq}.json`)
      writeFileSync(p, JSON.stringify({
        chat_id: 'FAKECHAT-1', message_id: seq, from_id: 'U1', scene: 'private',
        chat_type: 'private', ts: `2026-08-25T0${seq}:00:00.000Z`, text, ...extra,
      }))
      return p
    },
  }
}

test('空闲时连发三条_先攒波不入队_满窗后只入队一个合并件', () => {
  const r = mkBurstManager()
  r.at(1000); r.add('A1'); r.add('B2'); r.add('C3')
  r.m.drainInbox()
  expect(r.m.queue).toEqual([])            // 三条都在波里，一条都没进队
  r.at(6100); r.tick()
  expect(r.m.queue.length).toBe(1)
  const merged = JSON.parse(readFileSync(r.m.queue[0].path, 'utf8'))
  expect([merged.text, merged.chat_id, merged.message_id]).toEqual(['A1\nB2\nC3', 'FAKECHAT-1', 1])
})

test('攒波期间被兜底扫重复看见_同一原件不会重复入队', () => {
  const r = mkBurstManager()
  r.at(1000); r.add('A1'); r.add('B2')
  r.m.drainInbox(); r.m.drainInbox(); r.m.drainInbox()   // 5s 定时扫 + fs.watch 重复触发
  expect(r.m.queue).toEqual([])
  r.at(6100); r.tick()
  expect(r.m.queue.length).toBe(1)
  expect(JSON.parse(readFileSync(r.m.queue[0].path, 'utf8')).text).toBe('A1\nB2')
})

test('队列有积压时不攒波_照旧直接入队', () => {
  const r = mkBurstManager()
  r.m.queue.push({ kind: 'raw', content: '/compact', label: 'slash /compact' })
  r.at(1000); const a = r.add('A1')
  r.m.drainInbox()
  expect(r.m.queue.map((q: any) => q.path ?? q.label)).toEqual(['slash /compact', a])
})

test('burstWindowMs=0_关闭防抖_原件逐条直接入队', () => {
  const r = mkBurstManager(0)
  r.at(1000); const a = r.add('A1'); const b = r.add('B2')
  r.m.drainInbox()
  expect(r.m.queue.map((q: any) => q.path)).toEqual([a, b])
})

// ─── cliVersion：Windows .cmd 经 win_cmd.ts（与 worker/摘要同源，不再裸 cmd /c）────────────
type RunCall = { file: string; args: string[]; opts: any }
function fakeRun(stdout: unknown, calls: RunCall[]) {
  return ((file: string, args: string[], opts: any) => { calls.push({ file, args, opts }); return { stdout } }) as any
}

test('cliVersion viaCmd：cmd.exe /d /s /c + 整行外层引号 + verbatim（路径含括号/空格不截断）', () => {
  const calls: RunCall[] = []
  const bin = 'D:\\Tom (Work)\\npm\\claude.cmd'
  expect(cliVersion(bin, true, fakeRun('2.1.0 (Claude Code)\nextra line', calls))).toBe('2.1.0 (Claude Code)')
  expect(calls).toHaveLength(1)
  expect(calls[0].file).toBe('cmd.exe')
  expect(calls[0].args).toEqual(['/d', '/s', '/c', winCmdLine([bin, '--version'])])
  expect(calls[0].args[3]).toBe('""D:\\Tom (Work)\\npm\\claude.cmd" "--version""')
  expect(calls[0].opts.windowsVerbatimArguments).toBe(true)
  expect(calls[0].opts.encoding).toBe('utf8')
})

test('cliVersion 非 viaCmd：直接起 claude --version，不经 cmd、不带 verbatim', () => {
  const calls: RunCall[] = []
  expect(cliVersion('/opt/homebrew/bin/claude', false, fakeRun(' 1.0.0 \n', calls))).toBe('1.0.0')
  expect(calls[0].file).toBe('/opt/homebrew/bin/claude')
  expect(calls[0].args).toEqual(['--version'])
  expect(calls[0].opts.windowsVerbatimArguments).toBeUndefined()
})

test('cliVersion：stdout 空/缺失 → 空串（checkFingerprint 对空值不写指纹）', () => {
  expect(cliVersion('x.cmd', true, fakeRun('', []))).toBe('')
  expect(cliVersion('x', false, fakeRun(undefined, []))).toBe('')
})

test('cliVersion viaCmd：路径被 %…% 守卫拒绝 → 空串且不起子进程（spawnWorker 随后用同一原因 fail-loud）', () => {
  const calls: RunCall[] = []
  expect(cliVersion('C:\\Users\\%USERNAME%\\npm\\claude.cmd', true, fakeRun('9.9.9', calls))).toBe('')
  expect(calls).toHaveLength(0)
})

// ─── H-01 stripThinking（保行换占位）+ H-02 compactSessionIfHuge（估算/预算/对齐）────
// 白盒测试一律留在本文件（见文件头提示）：另起 *.test.ts 会因共享模块缓存污染 CHANNEL_DIR。
const H_DIRS: string[] = []
const mkH = () => { const d = mkdtempSync(join(tmpdir(), 'wm-h01-')); H_DIRS.push(d); return d }
afterEach(() => { for (const d of H_DIRS.splice(0)) rmSync(d, { recursive: true, force: true }) })
const jline = (o: unknown) => JSON.stringify(o)
const asstLine = (uuid: string, parentUuid: string | null, content: unknown[], ts?: string) =>
  ({ type: 'assistant', uuid, parentUuid, ...(ts ? { timestamp: ts } : {}), message: { role: 'assistant', content } })
const userLine2 = (uuid: string, parentUuid: string | null, text: string) =>
  ({ type: 'user', uuid, parentUuid, timestamp: new Date().toISOString(), message: { role: 'user', content: [{ type: 'text', text }] } })
const H_OLD = new Date(Date.now() - 13 * 3600_000).toISOString()
const H_NEW = new Date().toISOString()
const hNonEmpty = (p: string) => readFileSync(p, 'utf8').split('\n').filter(Boolean)
const hBlocks = (l: string) => JSON.parse(l).message.content

test('H01 纯thinking行保行换占位，uuid/parentUuid不变', () => {
  const p = join(mkH(), 's.jsonl')
  writeFileSync(p, [jline(asstLine('a1', null, [{ type: 'thinking', thinking: 'X' }], H_OLD)), jline(asstLine('a2', 'a1', [{ type: 'text', text: 'k' }]))].join('\n') + '\n')
  const r = stripThinking(p, 'all')
  expect(r.ok).toBe(true); expect(r.replaced).toBe(1)
  const a1 = hNonEmpty(p).find(l => l.includes('"a1"'))!
  const bs = hBlocks(a1)
  expect(bs.some((b: any) => b.type === 'thinking')).toBe(false)
  expect(bs.some((b: any) => b.type === 'text' && b.text.length > 0)).toBe(true)
  expect(JSON.parse(a1).parentUuid).toBeNull()
  expect(JSON.parse(hNonEmpty(p).find(l => l.includes('"a2"'))!).parentUuid).toBe('a1') // 后继引用不变 → 链未断
})

test('H01 12h 只动旧行，近期行逐字不变；缺时间戳不动', () => {
  const p = join(mkH(), 'r.jsonl')
  const recent = jline(asstLine('n', 'o', [{ type: 'thinking', thinking: 'N' }, { type: 'text', text: 'x' }], H_NEW))
  writeFileSync(p, [jline(asstLine('o', null, [{ type: 'thinking', thinking: 'O' }, { type: 'text', text: 'y' }], H_OLD)), recent, jline(asstLine('nt', null, [{ type: 'thinking', thinking: 'T' }]))].join('\n') + '\n')
  stripThinking(p, '12h')
  const after = hNonEmpty(p)
  expect(after.find(l => l.includes('"n"'))).toBe(recent)
  expect(hBlocks(after.find(l => l.includes('"o"'))!).some((b: any) => b.type === 'thinking')).toBe(false)
  expect(hBlocks(after.find(l => l.includes('"nt"'))!).some((b: any) => b.type === 'thinking')).toBe(true)
})

test('H01 幂等：第二次 replaced=0 且文件逐字不变', () => {
  const p = join(mkH(), 'i.jsonl')
  writeFileSync(p, jline(asstLine('a', null, [{ type: 'thinking', thinking: 'T' }, { type: 'text', text: 'v' }], H_OLD)) + '\n')
  stripThinking(p, 'all')
  const first = readFileSync(p)
  expect(stripThinking(p, 'all').replaced).toBe(0)
  expect(readFileSync(p).equals(first)).toBe(true)
})

test('H02 超预算压缩：保留区首行为真人 user 且 parentUuid=null', () => {
  const p = join(mkH(), 'big.jsonl')
  const ls: string[] = [jline({ type: 'system', subtype: 'compact_boundary' })]
  for (let i = 0; i < 80; i++) {
    ls.push(jline(userLine2('u' + i, i ? 'a' + (i - 1) : 'b0', 'q' + i + 'X'.repeat(6000))))
    ls.push(jline(asstLine('a' + i, 'u' + i, [{ type: 'text', text: 'a' + i + 'X'.repeat(6000) }])))
  }
  writeFileSync(p, ls.join('\n') + '\n')
  const r = compactSessionIfHuge(p)
  expect(r.ok).toBe(true); expect(r.dropped).toBeGreaterThan(0)
  const convs = hNonEmpty(p).map(l => JSON.parse(l)).filter(j => j.message?.role)
  expect(convs[0].message.role).toBe('user')
  expect(convs[0].parentUuid).toBeNull()
})

// ─── H2b：is_error 分类（纯函数）+ 在飞件名解析 ──────────────────────────────
test('H2b classify：结构化状态优先（429/5xx 可重试，其余 4xx 致命）', () => {
  expect(classifyResultError({ status: 500 })).toBe('retryable')
  expect(classifyResultError({ status: 503 })).toBe('retryable')
  expect(classifyResultError({ http_status: 429 })).toBe('retryable')
  expect(classifyResultError({ status: 401 })).toBe('fatal')
  expect(classifyResultError({ error: { status: 403 } })).toBe('fatal')
  expect(classifyResultError({ error: { type: 'AUTHENTICATION_ERROR' } })).toBe('fatal')
})

test('H2b classify：文本兜底要 HTTP/status 上下文，毫秒数不误判；判不出→可重试', () => {
  expect(classifyResultError({ message: 'HTTP 401 Unauthorized' })).toBe('fatal')
  expect(classifyResultError({ text: 'status 404 not found here' })).toBe('fatal')
  expect(classifyResultError({ text: 'took 4041 ms' })).toBe('retryable')       // 无 HTTP 上下文
  expect(classifyResultError({ text: 'connection reset ECONNRESET' })).toBe('retryable')
  expect(classifyResultError({ message: 'something odd happened' })).toBe('retryable')
  expect(classifyResultError({})).toBe('retryable')
  expect(classifyResultError({ text: 'model not found: gpt-x' })).toBe('fatal')
})

test('H2b parseInflightName：往返一致，非在飞名返回 null', () => {
  expect(parseInflightName('.inflight-0-1700000000000_1.part')).toEqual({ attempt: 0, stem: '1700000000000_1' })
  expect(parseInflightName('.inflight-2-ab-c.part')).toEqual({ attempt: 2, stem: 'ab-c' })
  expect(parseInflightName('1700000000000_1.json')).toBeNull()
  expect(parseInflightName('.pending')).toBeNull()
  expect(parseInflightName('.inflight-x-1.part')).toBeNull()   // attempt 非数字
})
