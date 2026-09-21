// Windows 起进程相关的纯函数：cmd.exe 命令行拼接（唯一实现；worker 与 /clear 摘要子进程共用）+ claude 可执行探测。
//
// 规则（缺一条就会在某类路径/参数上碎，2026-09-21 审查致命-1）：
//   1. 每个 token 都包双引号（空串也包，否则整个 token 消失）；
//   2. token 内的 " 写成 ""（cmd 只数引号奇偶，"" 不翻转引号态，& | < > ^ 才不会裸露）；
//   3. 紧邻 " 之前的反斜杠翻倍、token 结尾的反斜杠翻倍（UCRT：2N 个 \ + " → N 个 \）；
//   4. 整行再包一层外层引号 + `/d /s /c` + windowsVerbatimArguments：
//      cmd /s 的规则是"剥掉首个与最后一个引号、中间原样"，没有外层引号时，
//      首 token 一旦被引号包住（如 C:\Users\John Smith\...\claude.cmd），
//      命令名就被截成 C:\Users\John → "不是内部或外部命令"，worker 永远起不来。
//      /d 关掉注册表 AutoRun（clink 等会往 stdout 混输出）。
// 验收：node ~/.claude/skills/platform-compat-review/scripts/crt-roundtrip.mjs dispatcher/win_cmd.ts → 15/15。
// %…% 与 !…!（G-5）：cmd 在引号内照样展开，winCmdLine 检测到成对形态直接抛错（见 assertNoCmdExpansion）。
// 挡不住的（W-A3）：换行、8191 总长——调用方保证参数里没有这些。

import { existsSync } from 'fs'
import { win32 } from 'path'

export function quoteCmdArg(arg: string): string {
  let out = '"'
  let bs = 0
  for (const c of arg) {
    if (c === '\\') { bs++; continue }
    if (c === '"') { out += '\\'.repeat(bs * 2) + '""'; bs = 0; continue }
    out += '\\'.repeat(bs) + c
    bs = 0
  }
  return out + '\\'.repeat(bs * 2) + '"'
}

// %…% / !…! 守卫（G-5、W-A3）。cmd 在引号内照样展开 %VAR%（注册表 DelayedExpansion=1 的机器还展开 !VAR!），
// 命中就是静默换参数（CHANNEL_DIR 可由 HUB_CHANNELS_DIR 任意指定，用户名也可能含 %）。
// 选"拒绝并抛错"而不是转义，原因：cmd /c 命令行层没有可靠转义——%% 只在批处理里是转义、^ 在 % 展开之后才处理、
// ! 的转义又随 DelayedExpansion 开关变，写不出对两种机器都对的一个串；而这里的 token 全是路径
// （claude.cmd / CHANNEL_DIR / mcp 配置 / CLAUDE.md），改名或换 CLAUDE_BIN / HUB_CHANNELS_DIR 就能绕开，
// 错误信息直接说明改哪里。守卫是保守的：任何成对形态都拒（含 %%）。落单的 % 或 ! 不拦——cmd 对配不成对的
// 字符原样保留（落单 ! 只在 DelayedExpansion=1 时被吃，非默认，拦了就是对绝大多数机器的误报）。
const CMD_EXPANSION_RE = /%[^%]*%|![^!]*!/
export function assertNoCmdExpansion(token: string): void {
  const m = CMD_EXPANSION_RE.exec(token)
  if (!m) return
  throw new Error(
    `cmd.exe 会把 ${JSON.stringify(m[0])} 当环境变量展开（token: ${JSON.stringify(token)}）；` +
    'cmd /c 命令行层无可靠转义，请改路径避开成对的 % 与 !（CLAUDE_BIN、CHANNEL_DIR/HUB_CHANNELS_DIR 或 Windows 用户名）',
  )
}

/** [可执行, ...参数] → 交给 cmd /d /s /c 的整行（已含外层引号）；含 %…%/!…! 的 token 抛错（见上） */
export function winCmdLine(tokens: string[]): string {
  for (const t of tokens) assertNoCmdExpansion(t)
  return '"' + tokens.map(quoteCmdArg).join(' ') + '"'
}

export interface WinCmdSpawnSpec {
  file: string
  args: string[]
  opts: Record<string, unknown> & { windowsVerbatimArguments: true }
}

/** spawn(spec.file, spec.args, spec.opts) 即可；opts 原样透传并强制 windowsVerbatimArguments */
export function winCmdSpawnSpec(resolved: string, args: string[], opts: Record<string, unknown> = {}): WinCmdSpawnSpec {
  return {
    file: 'cmd.exe',
    args: ['/d', '/s', '/c', winCmdLine([resolved, ...args])],
    opts: { ...opts, windowsVerbatimArguments: true },
  }
}

// ─── claude 可执行探测（Windows）────────────────────────────────────────
// 不读 `where claude` 的 stdout：where.exe 往管道写的是 OEM 码页（中文 Windows 是 cp936）字节，
// 按 utf8 解码后用户名的中文段变成 U+FFFD → claude.cmd 路径不存在 → "不是内部或外部命令" → 无限退避重启。
// 改为拿 env（Node 以 Unicode 读环境变量，中文用户名无损）按 PATH 顺序逐目录 existsSync：
// 每目录试 .exe/.cmd/.bat（npm 的无扩展名 bash shim 天然跳过，直接 spawn 它会 WinError 193），
// 再兜底 npm 全局目录 %APPDATA%\npm 与官方原生安装器目录 %USERPROFILE%\.local\bin。
export function probeClaudeWin(
  env: Record<string, string | undefined>,
  exists: (p: string) => boolean = existsSync,
): string | null {
  const pathKey = Object.keys(env).find(k => k.toLowerCase() === 'path')
  const dirs = ((pathKey && env[pathKey]) || '').split(';').map(s => s.trim().replace(/^"|"$/g, '')).filter(Boolean)
  if (env.APPDATA) dirs.push(win32.join(env.APPDATA, 'npm'))
  if (env.USERPROFILE) dirs.push(win32.join(env.USERPROFILE, '.local', 'bin'))
  for (const d of dirs) {
    for (const ext of ['.exe', '.cmd', '.bat']) {
      const p = win32.join(d, 'claude' + ext)
      if (exists(p)) return p
    }
  }
  return null
}
