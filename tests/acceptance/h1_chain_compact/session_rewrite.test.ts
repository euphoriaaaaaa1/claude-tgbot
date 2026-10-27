// 黑盒验收 · H-01(剥离思考) + H-02(会话压缩)
// 只依据 .devflow/BRIEF-h1-chain-compact.md 与 INTERFACE-h1-chain-compact.md 的对
// 外契约。不读实现、不 import 具体内部逻辑，只按契约调用两个入口并观察"文件字节"
// 与"返回值"两个可观察面。全程只用 mkdtempSync(os.tmpdir())，绝不碰真实会话目录。
//
// 入口名：契约文档写作 stripOldThinking / compactIfTooLarge；本仓实际导出名可能不同，
// 这里做兼容取值（先插件/契约名，再回退实际名）。这是唯一从"运行时公共面"取的适配。
//
// 跑法：从仓库根 `bun test tests/acceptance/h1_chain_compact/`
// 红绿预期（bug 修复模式）：构造的复现用例当前应为红，修好后转绿。
import { test, expect, afterEach } from 'bun:test'
import {
  chmodSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// ─── 环境隔离：模块级目录先指到临时区，绝不落真实目录 ──────────────────────
process.env.CHANNEL_DIR ??= mkdtempSync(join(tmpdir(), 'h1cc-ch-'))
process.env.BOT_NAME ??= 'h1cc'

// 兼容契约名与实际导出名（运行时可观察的公共面）
const wm: any = await import('../../../dispatcher/worker-manager')
const stripThinking: any = wm.stripOldThinking ?? wm.stripThinking
const compactSessionIfHuge: any = wm.compactIfTooLarge ?? wm.compactSessionIfHuge

// ─── 夹具 ────────────────────────────────────────────────────────────────
const TMPDIRS: string[] = []
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0

function mkTmp(): string {
  const d = mkdtempSync(join(tmpdir(), 'h1cc-'))
  TMPDIRS.push(d)
  return d
}
/** 只读目录可能拦住清理：先恢复权限再递归删。 */
function restorePerms(d: string): void {
  try { chmodSync(d, 0o755) } catch { /* ignore */ }
  try { for (const e of readdirSync(d)) { try { chmodSync(join(d, e), 0o644) } catch { /* ignore */ } } } catch { /* ignore */ }
}
afterEach(() => {
  for (const d of TMPDIRS.splice(0)) { restorePerms(d); rmSync(d, { recursive: true, force: true }) }
})

const read = (p: string) => readFileSync(p, 'utf8')
const body = (p: string) => readFileSync(p)
const sha = (p: string) => new Bun.CryptoHasher('sha256').update(body(p)).digest('hex')
const nonEmptyLines = (p: string) => read(p).split('\n').filter(l => l.length > 0)
const uuidList = (p: string) => nonEmptyLines(p).map(l => { try { return JSON.parse(l).uuid } catch { return '__BAD__' } })
const parentList = (p: string) => nonEmptyLines(p).map(l => { try { return JSON.parse(l).parentUuid } catch { return '__BAD__' } })
const blocks = (line: string) => { const j = JSON.parse(line); return (j.message?.content ?? []) as any[] }
const hasThinking = (line: string) => blocks(line).some(b => b?.type === 'thinking')

const NOW = () => new Date().toISOString()
const OLD = (h = 13) => new Date(Date.now() - h * 3600_000).toISOString()

/** 造一行标准会话记录。 */
function rec(o: { uuid: string; parentUuid: string | null; timestamp?: string; role?: string; type?: string; content?: any[]; extra?: Record<string, unknown> }) {
  const r: any = {
    type: o.type ?? o.role ?? 'assistant',
    uuid: o.uuid,
    parentUuid: o.parentUuid,
    ...(o.timestamp ? { timestamp: o.timestamp } : {}),
    ...(o.role ? { message: { role: o.role, content: o.content ?? [] } } : {}),
    ...(o.extra ?? {}),
  }
  return JSON.stringify(r)
}
const thinking = (t: string) => ({ type: 'thinking', thinking: t })
const text = (t: string) => ({ type: 'text', text: t })

// ═══════════════════════════════════════════════════════════════════════════
// H-01 · stripThinking（剥离旧思考块）
// ═══════════════════════════════════════════════════════════════════════════

// ── 复现：唯一内容为 thinking 的行被整行删除，父链断裂 ──────────────────────
test('H01_复现_只有thinking的行_不删行_uuid列表逐字相等', () => {
  const d = mkTmp(), p = join(d, 's.jsonl')
  writeFileSync(p, [
    rec({ uuid: 'u1', parentUuid: null, role: 'user', type: 'user', content: [text('hi')] }),
    rec({ uuid: 'a1', parentUuid: 'u1', role: 'assistant', content: [thinking('ONLY')] }), // 只有思考
    rec({ uuid: 'a2', parentUuid: 'a1', role: 'assistant', content: [text('after')] }),
  ].join('\n') + '\n')
  const n0 = nonEmptyLines(p).length, u0 = uuidList(p), pu0 = parentList(p)

  stripThinking(p, 'all')

  expect(nonEmptyLines(p).length).toBe(n0)        // 行数一条不少
  expect(uuidList(p)).toEqual(u0)                  // uuid 列表逐字相等
  expect(parentList(p)).toEqual(pu0)               // parentUuid 引用关系不变
  expect(uuidList(p)).toContain('a1')              // 断点行仍在文件里
})

// ── 复现：12h 模式动了"近期"行 ────────────────────────────────────────────
test('H01_复现_12h模式_近期行逐字不变（仍含thinking）', () => {
  const d = mkTmp(), p = join(d, 'r.jsonl')
  const recent = rec({ uuid: 'new1', parentUuid: 'old1', timestamp: NOW(), role: 'assistant', content: [thinking('NEW'), text('n')] })
  writeFileSync(p, [
    rec({ uuid: 'old1', parentUuid: null, timestamp: OLD(13), role: 'assistant', content: [thinking('OLD'), text('o')] }),
    recent,
  ].join('\n') + '\n')

  stripThinking(p, '12h')

  const after = nonEmptyLines(p)
  const newLine = after.find(l => l.includes('"new1"'))!
  expect(newLine).toBe(recent)                     // 近期行原始字节不变
  const oldLine = after.find(l => l.includes('"old1"'))!
  expect(hasThinking(oldLine)).toBe(false)         // 旧行思考被清
})

// ── 复现：12h 模式动了"缺时间戳"的行 ──────────────────────────────────────
test('H01_复现_12h模式_缺时间戳行不动（仍含thinking）', () => {
  const d = mkTmp(), p = join(d, 'nt.jsonl')
  const noTs = rec({ uuid: 'noTs', parentUuid: null, role: 'assistant', content: [thinking('X'), text('t')] }) // 无 timestamp
  writeFileSync(p, noTs + '\n')
  stripThinking(p, '12h')
  const line = nonEmptyLines(p).find(l => l.includes('"noTs"'))!
  expect(hasThinking(line)).toBe(true)             // 宁可留思考，也不误删近期内容
})

// ── 正常路径：all 模式清除被替换行的 thinking，且保留非空 text ─────────────
test('H01_all模式_被替换行不再含thinking_且至少一个非空text', () => {
  const d = mkTmp(), p = join(d, 'a.jsonl')
  writeFileSync(p, rec({ uuid: 'a1', parentUuid: null, timestamp: OLD(), role: 'assistant', content: [thinking('SECRET'), text('ans')] }) + '\n')
  stripThinking(p, 'all')
  const line = nonEmptyLines(p).find(l => l.includes('"a1"'))!
  const bs = blocks(line)
  expect(bs.some(b => b?.type === 'thinking')).toBe(false)
  expect(bs.some(b => b?.type === 'text' && typeof b.text === 'string' && b.text.length > 0)).toBe(true)
})

// ── 正常路径：不含 thinking 的行零改动（逐字相等）────────────────────────
test('H01_不含量thinking的行_其它行逐字不变', () => {
  const d = mkTmp(), p = join(d, 'p.jsonl')
  const userLine = '{"type":"user",  "uuid":"u1", "parentUuid":null, "timestamp":"t", "message":{"role":"user","content":[{"type":"text","text":"hi"}]}}' // 异常空格
  const plain = rec({ uuid: 'a0', parentUuid: 'u1', role: 'assistant', content: [text('plain')] })
  writeFileSync(p, [userLine, plain, rec({ uuid: 'a1', parentUuid: 'a0', timestamp: OLD(), role: 'assistant', content: [thinking('T'), text('x')] })].join('\n') + '\n')
  stripThinking(p, 'all')
  const after = read(p)
  expect(after).toContain(userLine)                // 无 thinking 行原始字节保留
  expect(after).toContain(plain)
})

// ── 错误路径：坏行原样保留、不中断整轮 ────────────────────────────────────
test('H01_坏JSON行_原样保留且不中断后续替换', () => {
  const d = mkTmp(), p = join(d, 'bad.jsonl')
  const bad = 'NOT-JSON{{ broken'
  writeFileSync(p, [
    rec({ uuid: 'a1', parentUuid: null, timestamp: OLD(), role: 'assistant', content: [thinking('T1'), text('x')] }),
    bad,
    rec({ uuid: 'a2', parentUuid: 'a1', timestamp: OLD(), role: 'assistant', content: [thinking('T2'), text('y')] }),
  ].join('\n') + '\n')
  stripThinking(p, 'all')
  const after = read(p)
  expect(after).toContain(bad)                     // 坏行逐字保留
  const l1 = after.split('\n').find(l => l.includes('"a1"'))!
  const l2 = after.split('\n').find(l => l.includes('"a2"'))!
  expect(hasThinking(l1)).toBe(false)              // 坏行之后的仍被处理
  expect(hasThinking(l2)).toBe(false)
})

// ── 边界：空文件 / 文件不存在 ─────────────────────────────────────────────
test('H01_空文件与不存在文件_不抛且零动作', () => {
  const d = mkTmp(), empty = join(d, 'empty.jsonl')
  writeFileSync(empty, '')
  expect(() => stripThinking(empty, 'all')).not.toThrow()
  expect(read(empty)).toBe('')                     // 空文件保持空

  const missing = join(d, 'nope.jsonl')
  expect(() => stripThinking(missing, 'all')).not.toThrow()
  expect(existsSync(missing)).toBe(false)          // 不会凭空造文件
})

// ── ok 语义：成功返回 ok:true 且 replaced 计数 ───────────────────────────
test('H01_成功_返回ok_true且replaced等于被改行数', () => {
  const d = mkTmp(), p = join(d, 'ok.jsonl')
  writeFileSync(p, [
    rec({ uuid: 'a1', parentUuid: null, timestamp: OLD(), role: 'assistant', content: [thinking('T1'), text('x')] }),
    rec({ uuid: 'a2', parentUuid: 'a1', timestamp: OLD(), role: 'assistant', content: [thinking('T2'), text('y')] }),
  ].join('\n') + '\n')
  const r = stripThinking(p, 'all')
  expect(r?.ok).toBe(true)
  expect(r?.replaced).toBe(2)
})

// ── 失败语义：只读目录写回失败 → 原文件字节不变、不外抛 ───────────────────
test('H01_写回失败_原文件字节不变且不外抛', () => {
  if (isRoot) return
  const d = mkTmp(), p = join(d, 'ro.jsonl')
  writeFileSync(p, rec({ uuid: 'a1', parentUuid: null, timestamp: OLD(), role: 'assistant', content: [thinking('T'), text('x')] }) + '\n')
  const before = body(p)
  chmodSync(d, 0o555)                              // 目录只读 → 同目录临时文件建不出来
  expect(() => stripThinking(p, 'all')).not.toThrow()
  expect(body(p).equals(before)).toBe(true)        // 原文件字节不变
})

// ── 幂等：第二次 replaced=0 且文件逐字不变 ───────────────────────────────
test('H01_幂等_第二次不产生改动_文件逐字不变', () => {
  const d = mkTmp(), p = join(d, 'id.jsonl')
  writeFileSync(p, [
    rec({ uuid: 'a1', parentUuid: null, timestamp: OLD(), role: 'assistant', content: [thinking('T1'), text('x')] }),
    rec({ uuid: 'u1', parentUuid: 'a1', role: 'user', type: 'user', content: [text('hi')] }),
  ].join('\n') + '\n')
  stripThinking(p, 'all')
  const h1 = sha(p)
  stripThinking(p, 'all')                           // 第二次
  expect(sha(p)).toBe(h1)                           // 文件逐字不变（幂等）
})

// ═══════════════════════════════════════════════════════════════════════════
// H-02 · compactSessionIfHuge（会话压缩）
// ═══════════════════════════════════════════════════════════════════════════

const B = { type: 'system', subtype: 'compact_boundary' } as const
const bigText = (tag: string, n: number) => tag + 'X'.repeat(n)

/** 造一段"边界之后"的对话：n 轮 user/assistant，每行正文约 size 字符。 */
function writeConv(p: string, opts: { n?: number; size?: number; boundary?: boolean } = {}) {
  const { n = 80, size = 6000, boundary = true } = opts
  const ls: string[] = []
  if (boundary) ls.push(rec({ uuid: 'b0', parentUuid: null, timestamp: NOW(), type: 'system', extra: { subtype: 'compact_boundary' } }))
  let prev: string | null = boundary ? 'b0' : null
  for (let i = 0; i < n; i++) {
    const ru = 'u' + i, ra = 'a' + i
    ls.push(rec({ uuid: ru, parentUuid: prev, role: 'user', type: 'user', timestamp: NOW(), content: [text(bigText('q' + i, size))] }))
    ls.push(rec({ uuid: ra, parentUuid: ru, role: 'assistant', timestamp: NOW(), content: [text(bigText('a' + i, size))] }))
    prev = ra
  }
  writeFileSync(p, ls.join('\n') + '\n')
}
const convLines = (p: string) =>
  nonEmptyLines(p).map(l => { try { return JSON.parse(l) } catch { return null } })
    .filter(j => j && (j.message?.role === 'user' || j.message?.role === 'assistant'))

// ── 反向：未超预算的文件字节完全不变（sha 对比）──────────────────────────
test('H02_未超预算_文件sha完全不变', () => {
  const d = mkTmp(), p = join(d, 'small.jsonl')
  writeConv(p, { n: 3, size: 50 })
  const h0 = sha(p)
  expect(() => compactSessionIfHuge(p)).not.toThrow()
  expect(sha(p)).toBe(h0)                           // 不该压缩的绝不动一个字节
})

// ── 复现：保留区首行不是"真人 user 消息"，或父指针未清零 ──────────────────
test('H02_复现_保留区第一行须是真人user且parentUuid为null', () => {
  const d = mkTmp(), p = join(d, 'big.jsonl')
  writeConv(p, { n: 80, size: 6000 })
  compactSessionIfHuge(p)
  const convs = convLines(p)
  expect(convs.length).toBeGreaterThan(0)
  const first = convs[0]
  expect(first.message.role).toBe('user')                                  // 首行必须是真人消息
  expect(first.message.content.some((b: any) => b?.type === 'tool_result')).toBe(false)
  expect(first.parentUuid).toBeNull()                                      // 首行父指针清零
})

// ── 正常路径：压缩后至少保留 1 行对话（保底）─────────────────────────────
test('H02_压缩后_至少保留1行对话', () => {
  const d = mkTmp(), p = join(d, 'keep.jsonl')
  writeConv(p, { n: 80, size: 6000 })
  compactSessionIfHuge(p)
  expect(convLines(p).length).toBeGreaterThanOrEqual(1)
})

// ── 保留下限：保留区至少 8 行（现场行数足够时）──────────────────────────
test('H02_压缩后_保留区不少于8行', () => {
  const d = mkTmp(), p = join(d, 'min8.jsonl')
  writeConv(p, { n: 80, size: 6000 })
  compactSessionIfHuge(p)
  expect(nonEmptyLines(p).length).toBeGreaterThanOrEqual(8)
})

// ── 复现：边界之前的大量内容不应计入体积（否则会把整段旧历史带进来压）──────
test('H02_复现_边界前的大量内容不计入_体积未超则不动文件', () => {
  const d = mkTmp(), p = join(d, 'pre.jsonl')
  const ls = [
    rec({ uuid: 'pre', parentUuid: null, role: 'user', type: 'user', timestamp: NOW(), content: [text('Z'.repeat(1_800_000))] }), // 边界前巨量
    rec({ uuid: 'b0', parentUuid: 'pre', timestamp: NOW(), type: 'system', extra: { subtype: 'compact_boundary' } }),
    rec({ uuid: 'u9', parentUuid: 'b0', role: 'user', type: 'user', timestamp: NOW(), content: [text('hi')] }),
    rec({ uuid: 'a9', parentUuid: 'u9', role: 'assistant', timestamp: NOW(), content: [text('yo')] }),
  ]
  writeFileSync(p, ls.join('\n') + '\n')
  const h0 = sha(p)
  compactSessionIfHuge(p)
  expect(sha(p)).toBe(h0)                           // 边界之后很小 → 整轮不该压缩
})

// ── 反例：边界之后只有非对话行 → 找不到真人消息 → 放弃压缩、原文件不动 ─────
test('H02_边界后仅非对话行_放弃压缩_文件不变', () => {
  const d = mkTmp(), p = join(d, 'sys.jsonl')
  const ls = [rec({ uuid: 'b0', parentUuid: null, timestamp: NOW(), type: 'system', extra: { subtype: 'compact_boundary' } })]
  for (let i = 0; i < 60; i++) ls.push(rec({ uuid: 's' + i, parentUuid: i ? 's' + (i - 1) : 'b0', timestamp: NOW(), type: 'system', extra: { role: 'system', content: [text('S'.repeat(30000))] } }))
  writeFileSync(p, ls.join('\n') + '\n')
  const h0 = sha(p)
  compactSessionIfHuge(p)
  expect(sha(p)).toBe(h0)
})

// ── 复现：图片块含大 base64 但应按固定值计 → 不应触发压缩 ─────────────────
test('H02_复现_大base64图片块按固定值_不因体积高估而压缩', () => {
  const d = mkTmp(), p = join(d, 'img.jsonl')
  const b64 = 'A'.repeat(1_500_000)                 // 若按字符÷3计 → 约50万 token，远超阈值
  writeFileSync(p, [
    rec({ uuid: 'b0', parentUuid: null, timestamp: NOW(), type: 'system', extra: { subtype: 'compact_boundary' } }),
    rec({ uuid: 'u1', parentUuid: 'b0', role: 'user', type: 'user', timestamp: NOW(), content: [text('hello')] }),
    rec({ uuid: 'a1', parentUuid: 'u1', role: 'assistant', timestamp: NOW(), content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: b64 } }, text('saw it')] }),
  ].join('\n') + '\n')
  const h0 = sha(p)
  compactSessionIfHuge(p)
  expect(sha(p)).toBe(h0)                           // 图片按固定值计 → 体积不足 → 不动
})

// ── 边界：空文件 / 仅边界 → 不写盘（sha 不变）────────────────────────────
test('H02_空文件与仅边界_不写盘', () => {
  const d = mkTmp()
  const empty = join(d, 'empty.jsonl'); writeFileSync(empty, '')
  const e0 = sha(empty)
  expect(() => compactSessionIfHuge(empty)).not.toThrow()
  expect(sha(empty)).toBe(e0)

  const only = join(d, 'only.jsonl')
  writeFileSync(only, rec({ uuid: 'b0', parentUuid: null, timestamp: NOW(), type: 'system', extra: { subtype: 'compact_boundary' } }) + '\n')
  const o0 = sha(only)
  compactSessionIfHuge(only)
  expect(sha(only)).toBe(o0)
})

// ── ok 语义：成功返回 ok:true 且 kept/dropped/estimated 为数字 ─────────────
test('H02_成功_返回ok_true且计数为数字', () => {
  const d = mkTmp(), p = join(d, 'ok.jsonl')
  writeConv(p, { n: 80, size: 6000 })
  const r = compactSessionIfHuge(p)
  expect(r?.ok).toBe(true)
  expect(typeof r?.kept).toBe('number')
  expect(typeof r?.dropped).toBe('number')
  expect(typeof r?.estimated).toBe('number')
})

// ── 幂等：压缩到预算内后再调用不再改写 ───────────────────────────────────
test('H02_幂等_第二次不再改写_文件逐字不变', () => {
  const d = mkTmp(), p = join(d, 'idem.jsonl')
  writeConv(p, { n: 80, size: 6000 })
  compactSessionIfHuge(p)
  const h1 = sha(p)
  compactSessionIfHuge(p)
  expect(sha(p)).toBe(h1)
})

// ── 反向：夹在中间的超大单行被跳过而不终止，最近的小行仍保留 ──────────────
test('H02_中间超大单行_跳过不终止_最新行仍保留', () => {
  const d = mkTmp(), p = join(d, 'giant.jsonl')
  writeConv(p, { n: 20, size: 20 })                 // 先造正常轮次
  const ls = nonEmptyLines(p)
  // 把第 12 行的正文换成 30 万字符的巨行（夹在中间）
  const idx = 12
  const j = JSON.parse(ls[idx]); j.message.content = [text('Q'.repeat(300_000))]; ls[idx] = JSON.stringify(j)
  writeFileSync(p, ls.join('\n') + '\n')
  const lastUuid = JSON.parse(ls[ls.length - 1]).uuid
  compactSessionIfHuge(p)
  const after = nonEmptyLines(p).map(l => { try { return JSON.parse(l).uuid } catch { return null } })
  expect(after[after.length - 1]).toBe(lastUuid)    // 最新行（小行）没被丢掉
  expect(after.length).toBeGreaterThanOrEqual(8)    // 没被巨行一锅端
})
