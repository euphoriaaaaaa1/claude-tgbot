// pub-persona-dedup · dispatcher/worker_isolation.ts 的 prepareWorkerIsolation（行为清单 1-9）
// 只依据接口约定：claudeMdExcludes 新增 bot 目录自己的 CLAUDE.md，其余不变。
// 零副作用：全部在临时目录里搭，HOME 指向临时目录（import 前设好），不联网。
import { test, expect, afterAll } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'

// 先取临时目录的真实路径：macOS 的 /var 本身就是指向 /private/var 的链接，
// 不取真实路径的话，"无链接的普通目录"这个前提就不成立。
const BASE = mkdtempSync(join(realpathSync(tmpdir()), 'pp-dedup-'))
process.env.HOME = join(BASE, 'home')
mkdirSync(process.env.HOME, { recursive: true })
afterAll(() => { try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } })

const wi: any = await import('../../../dispatcher/worker_isolation')
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0
const isWin = process.platform === 'win32'

let seq = 0
function sandbox() {
  const root = join(BASE, `t${++seq}`)
  const home = join(root, 'home'), botDir = join(root, 'channels', 'bot1'), engineDir = join(root, 'engine', 'novelai-skill')
  mkdirSync(join(botDir, '.claude'), { recursive: true }); mkdirSync(home, { recursive: true })
  mkdirSync(engineDir, { recursive: true }); writeFileSync(join(engineDir, 'SKILL.md'), '# engine\n')
  return { root, home, botDir, engineDir }
}
// bot 目录经符号链接到达：root/link -> root/real，bot 目录 = root/link/channels/bot1
function linkedSandbox() {
  const root = join(BASE, `t${++seq}`)
  const real = join(root, 'real'), link = join(root, 'link')
  const home = join(root, 'home'), engineDir = join(root, 'engine', 'novelai-skill')
  mkdirSync(join(real, 'channels', 'bot1', '.claude'), { recursive: true }); mkdirSync(home, { recursive: true })
  mkdirSync(engineDir, { recursive: true }); writeFileSync(join(engineDir, 'SKILL.md'), '# engine\n')
  symlinkSync(real, link)
  const botDir = join(link, 'channels', 'bot1')
  return { root, home, botDir, engineDir, realBotDir: realpathSync(botDir) }
}
const layer = (data: Record<string, unknown> = {}, unreadable = false) => ({ data, unreadable })
const prep = (sb: any, extra: Record<string, unknown> = {}) =>
  wi.prepareWorkerIsolation({ botDir: sb.botDir, home: sb.home, user: layer(), engineDir: sb.engineDir, ...extra })
const excludesOf = (r: any): string[] => JSON.parse(readFileSync(r.flagFile, 'utf8')).claudeMdExcludes
const count = (list: string[], item: string) => list.filter(e => e === item).length

// ───────── 清单 1：普通目录 ─────────
test('排除列表_普通bot目录_含自己的CLAUDE_md且恰好一次', () => {
  const sb = sandbox(); const r = prep(sb)
  expect(r.status).toBe('ok')
  expect(count(excludesOf(r), join(sb.botDir, 'CLAUDE.md'))).toBe(1)
})

// ───────── 清单 2：经符号链接到达 ─────────
test.skipIf(isWin)('排除列表_bot目录经符号链接到达_链接路径与真实路径的CLAUDE_md各恰好一次', () => {
  const sb = linkedSandbox()
  expect(sb.realBotDir).not.toBe(sb.botDir)                       // 前提：两个路径确实不同
  const r = prep(sb); expect(r.status).toBe('ok')
  const ex = excludesOf(r)
  expect(count(ex, join(sb.botDir, 'CLAUDE.md'))).toBe(1)
  expect(count(ex, join(sb.realBotDir, 'CLAUDE.md'))).toBe(1)
})

// ───────── 清单 3：win32 两种分隔符 ─────────
test('排除列表_win32_bot目录CLAUDE_md的斜杠与反斜杠写法各恰好一次', () => {
  const sb = sandbox(); const ex = excludesOf(prep(sb, { platform: 'win32' }))
  const fwd = join(sb.botDir, 'CLAUDE.md')
  const back = fwd.replaceAll('/', '\\')
  expect(back).not.toBe(fwd)
  expect(count(ex, fwd)).toBe(1)
  expect(count(ex, back)).toBe(1)
})

// ───────── 清单 4：bot 目录下其他说明文件不排除 ─────────
for (const [label, parts] of [
  ['CLAUDE_local_md', ['CLAUDE.local.md']],
  ['点claude下的CLAUDE_md', ['.claude', 'CLAUDE.md']],
  ['点claude下的rules通配', ['.claude', 'rules', '**']],
] as const) {
  test(`排除列表_不含bot目录下的${label}`, () => {
    const sb = sandbox(); const ex = excludesOf(prep(sb))
    expect(ex).not.toContain(join(sb.botDir, ...parts))
  })
}

// ───────── 清单 5：共享目录自己的说明文件不排除 ─────────
test('排除列表_不含共享目录自己的CLAUDE_md', () => {
  const sb = sandbox(); const ex = excludesOf(prep(sb))
  expect(ex).not.toContain(join(sb.home, '.claude', 'worker-shared', 'CLAUDE.md'))
})

// ───────── 清单 6：祖先链照旧、无重复 ─────────
test('排除列表_祖先链照旧_用户级CLAUDE_md_父目录CLAUDE_md_父目录rules仍在', () => {
  const sb = sandbox(); const ex = excludesOf(prep(sb)); const parent = dirname(sb.botDir)
  expect(ex).toContain(join(sb.home, '.claude', 'CLAUDE.md'))
  expect(ex).toContain(join(parent, 'CLAUDE.md'))
  expect(ex).toContain(join(parent, '.claude', 'rules', '**'))
})
test('排除列表_无重复项', () => {
  const sb = sandbox(); const ex = excludesOf(prep(sb))
  expect(ex.length).toBe(new Set(ex).size)
})

// ───────── 清单 7：flag 文件其余内容不变 ─────────
test('flag文件_前三个键顺序仍是claudeMdExcludes_disableBundledSkills_skillOverrides', () => {
  const sb = sandbox(); const f = JSON.parse(readFileSync(prep(sb).flagFile, 'utf8'))
  expect(Object.keys(f).slice(0, 3)).toEqual(['claudeMdExcludes', 'disableBundledSkills', 'skillOverrides'])
  expect(f.disableBundledSkills).toBe(true)
  expect(f.skillOverrides).toEqual({ design: 'off', doctor: 'off', 'plugin-authoring': 'off' })
})

// ───────── 清单 8：幂等 ─────────
test('flag文件_同样输入准备两次_逐字节相同', () => {
  const sb = sandbox()
  const b1 = readFileSync(prep(sb).flagFile); const b2 = readFileSync(prep(sb).flagFile)
  expect(b2.equals(b1)).toBe(true)
})

// ───────── 清单 9：降级路径不变 ─────────
test.skipIf(isRoot || isWin)('降级_bot目录只读_flag写不成_返回degraded原因flag_write_failed_不留flag文件', () => {
  const sb = sandbox(); chmodSync(sb.botDir, 0o555)
  try {
    const r = prep(sb)
    expect(r.status).toBe('degraded'); expect(r.reason).toBe('flag_write_failed')
    expect(existsSync(join(sb.botDir, '.worker-settings.json'))).toBe(false)
    expect(readdirSync(sb.botDir).filter(n => n.startsWith('.worker-settings.json'))).toEqual([])
  } finally { chmodSync(sb.botDir, 0o755) }
})
