// win_cmd.ts 白盒：cmd /s 剥首尾引号 + UCRT parse_command_line 往返（与 platform-compat-review 的 crt-roundtrip.mjs 同规则）。
import { describe, expect, test } from 'bun:test'
import { quoteCmdArg, winCmdLine, winCmdSpawnSpec } from './win_cmd'

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
