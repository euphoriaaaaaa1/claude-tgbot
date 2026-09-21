// Windows 上经 cmd.exe 起 .cmd/.bat 的命令行拼接（唯一实现；worker 与 /clear 摘要子进程共用）。
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
// 挡不住的（W-A3）：%VAR% 展开、换行、8191 总长——调用方保证参数里没有这些。

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

/** [可执行, ...参数] → 交给 cmd /d /s /c 的整行（已含外层引号） */
export function winCmdLine(tokens: string[]): string {
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
