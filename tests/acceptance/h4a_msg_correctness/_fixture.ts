// H4a 消息与摘要正确性 —— 黑盒验收共享夹具（自带）
//
// 只依据 .devflow/BRIEF-h4a-msg-correctness.md 与 INTERFACE-h4a-msg-correctness.md 的对外契约。
// 不读实现、不碰真实会话目录/通道目录、不发任何网络请求：全部用 os.tmpdir() 下的自造短文件与注入。
//
// 命名假设（契约未给导出名，按本仓既有习惯做兼容取值，见各 pick 调用）：契约里出现的
// 事件名（clear_summary_bad_reply / clear_summary_empty_skip / clear_summary_extract_empty /
// hang_stale_removed / merge_write_failed / channel_wrap_escaped）是断言锚点；函数名做多别名回退。
//
// 单进程共享模块警告：本夹具不持有任何跨文件共享的临时目录，也不在 afterAll 删目录；
// 各测试文件用 mkTmp() 自建、自己 afterEach 清理。
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// ─── 临时目录：每个文件自管，绝不共享 ──────────────────────────────────────
export function mkTmp(prefix = 'h4a-'): string {
  return mkdtempSync(join(tmpdir(), prefix))
}
export function rmTmp(d: string): void {
  try { rmSync(d, { recursive: true, force: true }) } catch { /* ignore */ }
}

// ─── 事件捕获：本仓事件行惯例写 stderr（console.error 也落到 stderr）──────────
// 同步替换 process.stderr.write，跑完必还原；返回捕获到的所有文本行。
export function captureEvents(fn: () => void): string[] {
  const buf: string[] = []
  const orig = process.stderr.write.bind(process.stderr)
  ;(process.stderr as unknown as { write: (c: unknown) => boolean }).write = (c: unknown) => {
    buf.push(typeof c === 'string' ? c : String(c))
    return true
  }
  try {
    fn()
  } finally {
    ;(process.stderr as unknown as { write: unknown }).write = orig
  }
  return buf.join('').split('\n').map(s => s.trim()).filter(Boolean)
}

/** 事件计数：与注入 log 缓冲合并后统计（有的模块走注入 log，有的走 stderr）。 */
export function countEvent(lines: string[], needle: string): number {
  return lines.filter(l => l.includes(needle)).length
}

// ─── 契约名 → 实际导出名 的兼容取值 ────────────────────────────────────────
/** 从模块对象里按别名顺序取第一个存在的函数/值；都没有则返回 undefined。 */
export function pick<T = unknown>(mod: Record<string, unknown>, names: string[]): T | undefined {
  for (const n of names) {
    if (mod[n] !== undefined) return mod[n] as T
  }
  return undefined
}
/** 同 pick，但缺失时抛错（错误信息列出所有候选名，便于对齐实现）。 */
export function pickOrThrow<T = unknown>(mod: Record<string, unknown>, names: string[]): T {
  const v = pick<T>(mod, names)
  if (v === undefined) throw new Error(`契约函数缺失，候选名：${names.join(' / ')}`)
  return v
}

// ─── clear_summary 会话行构造（与既有 dispatcher/*.test.ts 同形）─────────────
export const userLine = (content: unknown): string =>
  JSON.stringify({ type: 'user', message: { role: 'user', content } })
export const asstLine = (...blocks: unknown[]): string =>
  JSON.stringify({ type: 'assistant', message: { role: 'assistant', content: blocks } })
export const txtBlock = (text: string) => ({ type: 'text', text })
export const replyBlock = (text: unknown, name = 'reply') => ({ type: 'tool_use', name, input: { text } })
export const channelUserLine = (chatId: string, body: string): string =>
  userLine(`<channel chat_id="${chatId}" message_id="1">${body}</channel>`)
