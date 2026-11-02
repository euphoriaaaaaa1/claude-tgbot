// pub-memo-restart · G：守卫压缩丢行后自动布防（行为清单 12-14，外加 armRecallAfterCompact 公开函数的两条直测）。
// 只依据 pub-memo-restart-接口.md 的对外约定：入口 compactAndArm(jsonl, botDir)（worker-manager 导出）、
// armRecallAfterCompact(botDir, nowMs)（recall_arm 导出），观察口 = 返回值 + 文件系统 + 子进程跑 Python 找回钩子。
// 零副作用：HOME / CHANNEL_DIR / bot 目录全在临时目录；不起 claude，不联网。
import { afterAll, afterEach, expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'
import {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'

// 先取真实 python 路径（pyenv 垫片依赖真实 HOME，下面会把 HOME 换成临时目录）
const PY = spawnSync('python3', ['-c', 'import sys;print(sys.executable)'], { encoding: 'utf8' }).stdout.trim() || 'python3'
const REPO = join(import.meta.dir, '..', '..', '..')
const BASE = mkdtempSync(join(tmpdir(), 'pmr-arm-'))
const HOME = join(BASE, 'home')
mkdirSync(HOME, { recursive: true })
Object.assign(process.env, {
  HOME, USERPROFILE: HOME, CHANNEL_DIR: join(BASE, 'channel'), BOT_NAME: 'pmrarm',
  BOT_NAMESPACE: '6ba7b810-9dad-11d1-80b4-00c04fd430c8', TELEGRAM_BOT_TOKEN: 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK',
  DISPATCHER_PORT: '18991', TELEGRAM_DISPATCHER_URL: 'http://127.0.0.1:18991', DISPATCHER_URL: 'http://127.0.0.1:18991',
  CLAUDE_BIN: join(BASE, 'no-such-claude'), CLAUDEBOTLIFE_TEST: '1', CLAUDEBOTLIFE_TEST_ROOT: BASE,
})
mkdirSync(process.env.CHANNEL_DIR!, { recursive: true })

const wm: any = await import('../../../dispatcher/worker-manager')
// recall_arm 在测试里按需导入：模块还不存在时只让直测两条红，不拖垮整个文件
const loadArm = async (): Promise<any> => await import('../../../dispatcher/recall_arm')

const isRoot = typeof process.getuid === 'function' && process.getuid() === 0
const isWin = process.platform === 'win32'
const HEAD = '【私聊记忆：'
const ISO_LINE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\n$/
const REPLY_TOOL = 'mcp__telegram-worker__reply'

// ─── 夹具 ────────────────────────────────────────────────────────────────
afterAll(() => { rmSync(BASE, { recursive: true, force: true }) })
const RESTORE: string[] = []
afterEach(() => { for (const d of RESTORE.splice(0)) { try { chmodSync(d, 0o755) } catch { /* ignore */ } } })

const SAMPLE = readFileSync(join(REPO, 'tests', 'fixtures', 'inbound_private.txt'), 'utf8')
const CHAT_ID = /chat_id="([^"]*)"/.exec(SAMPLE)![1]
const SAMPLE_T0 = Date.parse(/ ts="([^"]*)"/.exec(SAMPLE)![1])

/** 以样例为底造一条入站私聊文本：换正文、时刻、message_id。 */
function inbound(body: string, ts: string, mid: number): string {
  const t = SAMPLE.trimEnd()
  const i = t.lastIndexOf('<channel')
  const prefix = t.slice(0, i)
  const rest = t.slice(i)
  const tag = rest.slice(0, rest.indexOf('>') + 1)
    .replace(/ ts="[^"]*"/, ` ts="${ts}"`).replace(/ message_id="[^"]*"/, ` message_id="${mid}"`)
  return prefix + tag + '\n' + body + '\n</channel>'
}

/** 超过 170k 估算的会话：compact_boundary + 80 轮（每轮一条私聊 + 一条带大段正文的 bot 回复）。内容只生成一次，供多份拷贝用。 */
function bigSessionText(): string {
  const now = () => new Date().toISOString()
  const ls: string[] = [JSON.stringify({ type: 'system', subtype: 'compact_boundary', uuid: 'b0', parentUuid: null, timestamp: now() })]
  let prev = 'b0'
  for (let i = 0; i < 80; i++) {
    const ts = new Date(SAMPLE_T0 + i * 60_000).toISOString()
    ls.push(JSON.stringify({ type: 'user', uuid: 'u' + i, parentUuid: prev, timestamp: now(), message: { role: 'user', content: [{ type: 'text', text: inbound('私聊正文' + i, ts, 300 + i) }] } }))
    ls.push(JSON.stringify({ type: 'assistant', uuid: 'a' + i, parentUuid: 'u' + i, timestamp: now(), message: { role: 'assistant', content: [
      { type: 'text', text: 'a' + i + 'X'.repeat(12000) },
      { type: 'tool_use', id: 'toolu_a' + i, name: REPLY_TOOL, input: { chat_id: CHAT_ID, text: '回复' + i } }] } }))
    prev = 'a' + i
  }
  return ls.join('\n') + '\n'
}

/** 没超阈值的小会话。 */
function smallSessionText(): string {
  const now = () => new Date().toISOString()
  return [
    JSON.stringify({ type: 'system', subtype: 'compact_boundary', uuid: 'b0', parentUuid: null, timestamp: now() }),
    JSON.stringify({ type: 'user', uuid: 'u0', parentUuid: 'b0', timestamp: now(), message: { role: 'user', content: [{ type: 'text', text: inbound('一条小消息', new Date(SAMPLE_T0).toISOString(), 1) }] } }),
  ].join('\n') + '\n'
}

let seq = 0
type Case = { jsonl: string; botDir: string; claudeDir: string }
/** 一份会话 + 一个 bot 目录（会话放在 HOME/.claude/projects 下，钩子才认）。 */
function mkCase(text: string, opts: { dotClaude?: boolean } = {}): Case {
  const n = ++seq
  const botDir = join(BASE, 'bots', 'case' + n)
  const proj = join(HOME, '.claude', 'projects', '-case' + n)
  mkdirSync(botDir, { recursive: true }); mkdirSync(proj, { recursive: true })
  if (opts.dotClaude !== false) mkdirSync(join(botDir, '.claude'))
  const jsonl = join(proj, '33333333-4444-5555-6666-77777777777' + (n % 10) + '.jsonl')
  writeFileSync(jsonl, text)
  return { jsonl, botDir, claudeDir: join(botDir, '.claude') }
}

const uuids = (p: string) => readFileSync(p, 'utf8').split('\n').filter(Boolean).map(l => { try { return JSON.parse(l).uuid } catch { return '__BAD__' } })

/** 返回值同形：键相同；数字/布尔/null 字段逐值相等；字符串字段（路径、时间戳）只比类型。 */
function expectSameShape(actual: any, expected: any) {
  expect(Object.keys(actual).sort()).toEqual(Object.keys(expected).sort())
  for (const k of Object.keys(expected)) {
    if (typeof expected[k] === 'string') expect(typeof actual[k]).toBe('string')
    else expect(actual[k]).toEqual(expected[k])
  }
}

function runRecall(c: Case) {
  const r = spawnSync(PY, [join(REPO, 'scripts', 'recall_private.py')], {
    input: JSON.stringify({ hook_event_name: 'SessionStart', session_id: 'sess-1', source: 'resume', cwd: c.botDir, transcript_path: c.jsonl }),
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME, USERPROFILE: HOME, TZ: 'UTC', PYTHONDONTWRITEBYTECODE: '1' },
    encoding: 'utf8',
  })
  return { rc: r.status, out: r.stdout, err: r.stderr }
}

const armedOf = (c: Case) => join(c.claudeDir, 'compact-recall.armed')
const mtimeNs = (p: string) => statSync(p, { bigint: true }).mtimeNs
const leftoverTmp = (c: Case) => readdirSync(c.claudeDir).filter(n => n.startsWith('.compact-recall.armed.') && n.endsWith('.tmp'))

// ═══ #12 ═══════════════════════════════════════════════════════════════════
test('G12_压缩真丢了老对话_布防_返回值与单调compactSessionIfHuge一致_Python钩子能补回', () => {
  const text = bigSessionText()
  const solo = mkCase(text), both = mkCase(text)
  const soloOut = wm.compactSessionIfHuge(solo.jsonl)
  expect(soloOut?.ok).toBe(true)
  expect(soloOut?.dropped).toBeGreaterThan(0)                 // 夹具自检：这份会话确实会丢行
  const before = Date.now()
  const out = wm.compactAndArm(both.jsonl, both.botDir)
  expect(out?.ok).toBe(true)
  expect(out?.dropped).toBeGreaterThan(0)
  expectSameShape(out, soloOut)
  expect(uuids(both.jsonl)).toEqual(uuids(solo.jsonl))        // 会话文件结果与单调一致
  const armed = armedOf(both)
  expect(existsSync(armed)).toBe(true)
  const content = readFileSync(armed, 'utf8')
  expect(content).toMatch(ISO_LINE)
  expect(Math.abs(Date.parse(content.trim()) - before)).toBeLessThan(120_000)
  if (!isWin) expect(statSync(armed).mode & 0o777).toBe(0o600)
  expect(leftoverTmp(both)).toEqual([])
  const hook = runRecall(both)
  expect(hook.rc).toBe(0)
  expect(hook.out.startsWith(HEAD)).toBe(true)
})

// ═══ #13 ═══════════════════════════════════════════════════════════════════
const OLD_ARMED = '2026-11-01T00:00:00.000Z\n'
function withOldArmed(c: Case) { if (existsSync(c.claudeDir)) writeFileSync(armedOf(c), OLD_ARMED, { mode: 0o600 }) }
function armedSnapshot(c: Case) { return existsSync(armedOf(c)) ? { text: readFileSync(armedOf(c), 'utf8'), ns: mtimeNs(armedOf(c)) } : null }

test('G13_没超阈值_不布防_已有armed不动_结果与单调一致', () => {
  const text = smallSessionText()
  const solo = mkCase(text), both = mkCase(text)
  const soloOut = wm.compactSessionIfHuge(solo.jsonl)
  withOldArmed(both)
  const snap = armedSnapshot(both)
  const out = wm.compactAndArm(both.jsonl, both.botDir)
  expectSameShape(out, soloOut)
  expect(readFileSync(both.jsonl, 'utf8')).toBe(readFileSync(solo.jsonl, 'utf8'))
  expect(armedSnapshot(both)).toEqual(snap)
  expect(leftoverTmp(both)).toEqual([])
})

for (const kind of ['文件', '目录', '符号链接'] as const) {
  test(`G13_auto-off开关是${kind}_不布防_已有armed不动_结果与单调一致`, () => {
    const text = bigSessionText()
    const solo = mkCase(text), both = mkCase(text)
    const soloOut = wm.compactSessionIfHuge(solo.jsonl)
    withOldArmed(both)
    const off = join(both.claudeDir, 'compact-recall.auto-off')
    if (kind === '文件') writeFileSync(off, '')
    else if (kind === '目录') mkdirSync(off)
    else symlinkSync(join(BASE, 'no-such-target'), off)       // 悬空链接也算（lstat 成功即关）
    const snap = armedSnapshot(both)
    const out = wm.compactAndArm(both.jsonl, both.botDir)
    expectSameShape(out, soloOut)
    expect(uuids(both.jsonl)).toEqual(uuids(solo.jsonl))
    expect(armedSnapshot(both)).toEqual(snap)
    expect(leftoverTmp(both)).toEqual([])
  })
}

test('G13_bot目录下没有dotclaude_不抛_不建目录_结果与单调一致', () => {
  const text = bigSessionText()
  const solo = mkCase(text), both = mkCase(text, { dotClaude: false })
  const soloOut = wm.compactSessionIfHuge(solo.jsonl)
  const out = wm.compactAndArm(both.jsonl, both.botDir)
  expectSameShape(out, soloOut)
  expect(uuids(both.jsonl)).toEqual(uuids(solo.jsonl))
  expect(existsSync(join(both.botDir, '.claude'))).toBe(false)
})

test.skipIf(isWin || isRoot)('G13_dotclaude只读_写不进去_不抛_已有armed不动_不留临时文件', () => {
  const text = bigSessionText()
  const solo = mkCase(text), both = mkCase(text)
  const soloOut = wm.compactSessionIfHuge(solo.jsonl)
  withOldArmed(both)
  const snap = armedSnapshot(both)
  RESTORE.push(both.claudeDir)
  chmodSync(both.claudeDir, 0o500)
  const out = wm.compactAndArm(both.jsonl, both.botDir)
  chmodSync(both.claudeDir, 0o755)
  expectSameShape(out, soloOut)
  expect(uuids(both.jsonl)).toEqual(uuids(solo.jsonl))
  expect(armedSnapshot(both)).toEqual(snap)
  expect(leftoverTmp(both)).toEqual([])
})

// ═══ #14 ═══════════════════════════════════════════════════════════════════
test('G14_已有旧armed和count3的inflight_再次压缩丢行_armed被重写_钩子重新补回且count回到1', () => {
  const c = mkCase(bigSessionText())
  writeFileSync(armedOf(c), OLD_ARMED.replace('2026-11-01', new Date().toISOString().slice(0, 10)), { mode: 0o600 })
  const oldNs = mtimeNs(armedOf(c))
  const inflight = join(c.claudeDir, 'compact-recall.inflight')
  writeFileSync(inflight, `{"t": ${Date.now() / 1000 - 100}, "count": 3, "armed_ns": ${oldNs}}\n`, { mode: 0o600 })
  const out = wm.compactAndArm(c.jsonl, c.botDir)
  expect(out?.ok).toBe(true)
  expect(out?.dropped).toBeGreaterThan(0)
  expect(mtimeNs(armedOf(c))).not.toBe(oldNs)                 // armed 被重写，修改时刻变了
  expect(readFileSync(armedOf(c), 'utf8')).toMatch(ISO_LINE)
  const hook = runRecall(c)
  expect(hook.rc).toBe(0)
  expect(hook.out.startsWith(HEAD)).toBe(true)
  const raw = readFileSync(inflight, 'utf8')
  expect(raw).toMatch(/"count":\s*1\b/)                       // 旧 inflight 随之作废，重新从 1 计
  expect(BigInt(/"armed_ns":\s*(\d+)/.exec(raw)![1])).toBe(mtimeNs(armedOf(c)))
})

// ═══ armRecallAfterCompact 公开函数直测（6.4a）═══════════════════════════════
test('armRecallAfterCompact_写一行ISO时刻_权限0600_返回armed_再调一次覆盖刷新', async () => {
  const ra = await loadArm()
  const c = mkCase(smallSessionText())
  const nowMs = Date.UTC(2026, 10, 3, 1, 2, 3, 456)
  expect(ra.armRecallAfterCompact(c.botDir, nowMs)).toBe('armed')
  expect(readFileSync(armedOf(c), 'utf8')).toBe('2026-11-03T01:02:03.456Z\n')
  if (!isWin) expect(statSync(armedOf(c)).mode & 0o777).toBe(0o600)
  expect(ra.armRecallAfterCompact(c.botDir, nowMs + 1000)).toBe('armed')
  expect(readFileSync(armedOf(c), 'utf8')).toBe('2026-11-03T01:02:04.456Z\n')
  expect(leftoverTmp(c)).toEqual([])
})

test.skipIf(isWin)('armRecallAfterCompact_已有armed是目录_改名覆盖失败_返回failed_目录原样_不留临时文件', async () => {
  const ra = await loadArm()
  const c = mkCase(smallSessionText())
  mkdirSync(armedOf(c))
  expect(ra.armRecallAfterCompact(c.botDir, Date.now())).toBe('failed')
  expect(lstatSync(armedOf(c)).isDirectory()).toBe(true)
  expect(leftoverTmp(c)).toEqual([])
})
