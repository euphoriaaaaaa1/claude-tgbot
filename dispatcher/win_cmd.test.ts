// win_cmd.ts 白盒：cmd /s 剥首尾引号 + UCRT parse_command_line 往返（与 platform-compat-review 的 crt-roundtrip.mjs 同规则）。
import { describe, expect, test } from 'bun:test'
import { readdirSync, readFileSync } from 'fs'
import { join } from 'path'
import { quoteCmdArg, winCmdLine, winCmdSpawnSpec, probeClaudeWin, assertNoCmdExpansion } from './win_cmd'

// UCRT 规则复刻：2N 个 \ + " → N 个 \ 并翻转引号态；2N+1 个 \ + " → N 个 \ + 字面 "；引号态内 "" → 字面 "
function crtParse(cmdline: string): string[] {
  const out: string[] = []; let i = 0, inQ = false, cur = '', started = false
  while (i < cmdline.length) {
    const c = cmdline[i]
    if (!inQ && (c === ' ' || c === '\t')) { if (started) { out.push(cur); cur = ''; started = false } i++; continue }
    started = true
    let bs = 0; while (cmdline[i] === '\\') { i++; bs++ }
    if (cmdline[i] === '"') {
      let copy = true
      if (bs % 2 === 0) { if (inQ && cmdline[i + 1] === '"') i++; else { copy = false; inQ = !inQ } }
      cur += '\\'.repeat(Math.floor(bs / 2)); if (copy) cur += '"'; i++
    } else { cur += '\\'.repeat(bs); if (i < cmdline.length) { cur += cmdline[i]; i++ } }
  }
  if (started) out.push(cur)
  return out
}
// cmd /s：首字符为 " → 去掉首个与最后一个 "，中间原样
const cmdStrip = (s: string) => (s.startsWith('"') && s.endsWith('"') ? s.slice(1, -1) : s)
const roundtrip = (toks: string[]) => crtParse(cmdStrip(winCmdLine(toks)))

describe('quoteCmdArg', () => {
  test('每 token 都包引号，空串也包', () => {
    expect(quoteCmdArg('plain')).toBe('"plain"')
    expect(quoteCmdArg('')).toBe('""')
  })
  test('内嵌引号写成 ""，其前反斜杠翻倍；结尾反斜杠翻倍', () => {
    expect(quoteCmdArg('say "hi"')).toBe('"say ""hi"""')
    expect(quoteCmdArg('a\\"b')).toBe('"a\\\\""b"')
    expect(quoteCmdArg('D:\\data\\')).toBe('"D:\\data\\\\"')
  })
  test('引号奇偶守恒：每个 token 的引号数为偶数（cmd 不会把 & | < > 读成元字符）', () => {
    for (const t of ['a"&b', 'x|y"z', 'a\\"b', 'a"\\', '"'])
      expect((quoteCmdArg(t).match(/"/g) || []).length % 2).toBe(0)
  })
})

describe('winCmdLine 往返', () => {
  const CASES: Array<[string, string[]]> = [
    ['用户名含空格（致命-1 原案）', ['C:\\Users\\John Smith\\AppData\\Roaming\\npm\\claude.cmd', '-p', '--add-dir', 'C:\\Users\\John Smith\\.claude\\channels\\bot']],
    ['CHANNEL_DIR 带尾反斜杠', ['C:\\npm\\claude.cmd', '--add-dir', 'C:\\bots\\chenlulu\\']],
    ['仓路径含括号', ['D:\\Tom (Work)\\npm\\claude.cmd', '--session-id', 'x']],
    ['元字符/空格/内嵌引号', ['C:\\npm\\claude.cmd', 'mcp<2', 'a&b', 'x|y', 'p^q', 'has space', 'say "hi"', 'next']],
    ['空串 token', ['C:\\npm\\claude.cmd', '', 'next']],
    ['尾 3 个 \\', ['C:\\npm\\claude.cmd', 'D:\\a\\\\\\', 'next']],
    ['JSON 参数', ['C:\\npm\\claude.cmd', '--config', '{"k":"v\\\\w","n":1}', 'next']],
    ['a\\"、a"\\、单引号 token', ['C:\\npm\\claude.cmd', 'a\\"', 'a"\\', '"', 'next']],
  ]
  for (const [name, toks] of CASES) test(name, () => expect(roundtrip(toks)).toEqual(toks))

  test('整行以外层引号包住：cmd /s 剥掉后命令名仍完整（不再截成 C:\\Users\\John）', () => {
    const line = winCmdLine(['C:\\Users\\John Smith\\npm\\claude.cmd', '-p'])
    expect(line.startsWith('""C:\\Users\\John Smith')).toBe(true)
    expect(crtParse(cmdStrip(line))[0]).toBe('C:\\Users\\John Smith\\npm\\claude.cmd')
  })
})

describe('winCmdSpawnSpec', () => {
  test('cmd.exe /d /s /c <整行> + windowsVerbatimArguments，opts 透传', () => {
    const s = winCmdSpawnSpec('C:\\npm\\claude.cmd', ['-p'], { cwd: 'C:\\x', stdio: ['pipe', 'pipe', 'pipe'] })
    expect(s.file).toBe('cmd.exe')
    expect(s.args.slice(0, 3)).toEqual(['/d', '/s', '/c'])
    expect(s.args[3]).toBe('""C:\\npm\\claude.cmd" "-p""')
    expect(s.opts.windowsVerbatimArguments).toBe(true)
    expect(s.opts.cwd).toBe('C:\\x')
  })
})

describe('probeClaudeWin（不解码 where 输出，中文用户名无损）', () => {
  const home = 'C:\\Users\\张三'
  const env = { Path: `C:\\Windows\\system32;"${home}\\AppData\\Roaming\\npm"`, APPDATA: `${home}\\AppData\\Roaming`, USERPROFILE: home }
  test('按 PATH 顺序找 .exe/.cmd/.bat，跳过无扩展名 bash shim', () => {
    const fs = new Set([`${home}\\AppData\\Roaming\\npm\\claude`, `${home}\\AppData\\Roaming\\npm\\claude.cmd`])
    expect(probeClaudeWin(env, p => fs.has(p))).toBe(`${home}\\AppData\\Roaming\\npm\\claude.cmd`)
  })
  test('PATH 没刷新时兜底 %USERPROFILE%\\.local\\bin\\claude.exe（官方安装器）', () => {
    const fs = new Set([`${home}\\.local\\bin\\claude.exe`])
    expect(probeClaudeWin({ PATH: 'C:\\Windows', USERPROFILE: home }, p => fs.has(p))).toBe(`${home}\\.local\\bin\\claude.exe`)
  })
  test('都没有 → null（交给 PATH，spawn error 里报清楚）', () => {
    expect(probeClaudeWin(env, () => false)).toBeNull()
  })
})

describe('cmd.exe 消费者收敛（W-A2：修一条路漏另一条路——cliVersion 曾漏网）', () => {
  test('dispatcher/*.ts 源码里除 win_cmd.ts 外没有裸 spawn("cmd"…) / ["/c", …]', () => {
    const src = readdirSync(import.meta.dir).filter(f => f.endsWith('.ts') && !f.endsWith('.test.ts') && f !== 'win_cmd.ts')
    expect(src.length).toBeGreaterThan(5)
    const hits: string[] = []
    for (const f of src) {
      readFileSync(join(import.meta.dir, f), 'utf8').split('\n').forEach((line, i) => {
        if (/\bspawn(?:Sync)?\(\s*['"`]cmd(?:\.exe)?['"`]/.test(line) || /['"`]\/[cC]['"`]\s*,/.test(line)) hits.push(`${f}:${i + 1}: ${line.trim()}`)
      })
    }
    expect(hits).toEqual([])
  })
})

describe('%…% / !…! 守卫（G-5：cmd 在引号内照样展开，命中即静默换参数 → 拒绝并抛错）', () => {
  test('成对 % 或 ! → 抛错，错误里带被拒的片段', () => {
    expect(() => winCmdLine(['C:\\npm\\claude.cmd', '--add-dir', 'C:\\Users\\a%b%c\\.claude\\channels\\bot'])).toThrow(/%b%/)
    expect(() => winCmdLine(['C:\\Users\\%USERNAME%\\npm\\claude.cmd', '-p'])).toThrow(/%USERNAME%/)
    expect(() => winCmdLine(['C:\\npm\\claude.cmd', 'D:\\!x!\\bots'])).toThrow(/!x!/)
    expect(() => assertNoCmdExpansion('%TEMP%')).toThrow(/%TEMP%/)
  })
  test('winCmdSpawnSpec 同样拦（worker / 摘要 / cliVersion 三处都经它）', () => {
    expect(() => winCmdSpawnSpec('C:\\npm\\claude.cmd', ['--add-dir', 'C:\\%TEMP%\\x'])).toThrow(/%TEMP%/)
  })
  test('落单的 % 或 !（cmd 原样保留）不拦，正常路径不受影响', () => {
    for (const t of ['C:\\Users\\100%\\bots', 'D:\\wow!\\x', '50% done!', '%', '!', 'C:\\Users\\John Smith\\.claude'])
      expect(() => winCmdLine(['C:\\npm\\claude.cmd', t])).not.toThrow()
    expect(() => assertNoCmdExpansion('')).not.toThrow()
  })
  test('错误信息说明改哪里（CLAUDE_BIN / CHANNEL_DIR）', () => {
    let msg = ''
    try { winCmdLine(['C:\\%X%\\claude.cmd']) } catch (e) { msg = (e as Error).message }
    expect(msg).toContain('%X%')
    expect(msg).toMatch(/CLAUDE_BIN/)
    expect(msg).toMatch(/CHANNEL_DIR/)
  })
})
