// H4a · H-23（清空摘要取"真正发给用户的话"）+ H-24（空摘要不得覆盖旧摘要）
//
// 只依据 BRIEF/INTERFACE。黑盒：只调对外入口、只看返回值/文件字节/事件行。
// 复现语义（bug 修复模式）：标注 [复现] 的用例当前应为红，修好后转绿。
//
// reduce 风险说明：契约未给"清空流程"函数名，本文件用多别名回退（pickOrThrow）。
// 若流程函数名与实现不一致，会以"契约函数缺失"红，属预期内的名字待对齐，不是行为不一致。
import { test, expect, afterEach } from 'bun:test'
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  mkTmp, rmTmp, captureEvents, countEvent, pickOrThrow,
  userLine, asstLine, txtBlock, replyBlock, channelUserLine,
} from './_fixture'

// ─── 隔离：动态 import 前先钉死 env，绝不碰真实目录（与本仓 h1/h2a 验收同源策略）──
process.env.CHANNEL_DIR ??= mkTmp('h4a-ch-')
process.env.BOT_NAME ??= 'h4a'

const clearMod: Record<string, unknown> = await import('../../../dispatcher/clear_summary')
const extractRecentTurns: any = clearMod.extractRecentTurns

const DIRS: string[] = []
const mk = (p = 'h4a-') => { const d = mkTmp(p); DIRS.push(d); return d }
afterEach(() => { for (const d of DIRS.splice(0)) rmTmp(d) })

const assistantText = (jsonl: string): string | undefined =>
  extractRecentTurns(jsonl).find((t: any) => t.role === 'assistant')?.text
const turns = (jsonl: string): any[] => extractRecentTurns(jsonl)

// ═══════════════════════════════════════════════════════════════════════════
// H-23 · reply 提取
// ═══════════════════════════════════════════════════════════════════════════

test('H23[复现]_助手行含text块和reply调用_摘要只取reply原文不含text块', () => {
  const line = asstLine(txtBlock('这段是内部草稿不算'), replyBlock('真正发给用户的话'))
  expect(assistantText(line)).toBe('真正发给用户的话')
})

test('H23[复现]_同一行两个reply_按出现顺序换行拼接', () => {
  const line = asstLine(replyBlock('第一句'), replyBlock('第二句'))
  expect(assistantText(line)).toBe('第一句\n第二句')
})

test('H23[复现]_同一句话回两次_记两次不去重', () => {
  const line = asstLine(replyBlock('一模一样'), replyBlock('一模一样'))
  expect(assistantText(line)).toBe('一模一样\n一模一样')
})

test('H23[复现]_恰名判定_reply_comment与reply_all都不算_只取恰为reply的', () => {
  const line = asstLine(replyBlock('应当留下', 'reply'), replyBlock('忽略甲', 'reply_comment'), replyBlock('忽略乙', 'reply_all'))
  expect(assistantText(line)).toBe('应当留下')
})

test('H23[复现]_某次reply的input_text非字符串_跳过该次_其余照取', () => {
  const line = asstLine(replyBlock(12345 as unknown, 'reply'), replyBlock('第二个有效'))
  expect(assistantText(line)).toBe('第二个有效')
})

test('H23[复现]_只有reply_comment加text块_回退到text块（非恰名不劫持）', () => {
  // 相邻回归：没有恰名 reply 时，既有 text 块提取行为保持不变
  const line = asstLine(replyBlock('X', 'reply_comment'), txtBlock('真文本'))
  expect(assistantText(line)).toBe('真文本')
})

test('H23_既有基线_无恰名reply时_纯text块取text_无文本无reply则为空', () => {
  expect(assistantText(asstLine(txtBlock('普通回答')))).toBe('普通回答')   // 纯text块照取（不回归）
  expect(turns(asstLine({ type: 'tool_use', name: 'Bash', input: {} }))).toEqual([])  // 既无text也无reply → 空
})

test('H23[复现]_text缺失的reply记clear_summary_bad_reply事件', () => {
  const line = asstLine(replyBlock(undefined as unknown, 'reply'), replyBlock('有效'))
  const events = captureEvents(() => { assistantText(line) })
  expect(countEvent(events, 'clear_summary_bad_reply')).toBeGreaterThanOrEqual(1)
  expect(events.some(l => /clear_summary_bad_reply\s+n=\d+/.test(l))).toBe(true)
})

// ═══════════════════════════════════════════════════════════════════════════
// H-24 · 空摘要不得覆盖旧摘要
// ═══════════════════════════════════════════════════════════════════════════

const OLD_SUMMARY = '# 最近对话摘要\n\n更新：2026-01-01\n\n上一次的旧摘要内容\n'
// 契约未固定导出名 → 多别名回退（clear_summary 优先，回退 worker-manager）。
// 懒解析：缺失时只红 H-24 组，不影响 H-23 组的独立判定。
const wmMod: Record<string, unknown> = await import('../../../dispatcher/worker-manager')
const getClearFlow = (): any => pickOrThrow(
  { ...clearMod, ...wmMod },
  ['runClearSummary', 'clearAndSummarize', 'summarizeAndStore', 'performClearSummary',
   'writeClearSummary', 'clearSummaryFlow', 'summarizeForClear', 'clearSummary'],
)

/** 造一份带旧摘要的记忆目录，返回 {dir, snap} —— snap 是整棵树的逐字快照。 */
function seedMemory() {
  const dir = mk('h4a-mem-')
  mkdirSync(join(dir, 'memory'), { recursive: true })
  writeFileSync(join(dir, 'memory', 'recent_conversation.md'), OLD_SUMMARY)
  writeFileSync(join(dir, 'MEMORY.md'), '# Memory Index\n\n## 最近对话\n')
  return { dir, snap: snapshot(dir) }
}
/** 路径无关快照：整棵树 relpath -> 内容，用于断言"文件逐字不变"。 */
function snapshot(dir: string): Record<string, string> {
  const out: Record<string, string> = {}
  const walk = (base: string, rel: string) => {
    for (const name of readdirSync(join(base, rel))) {
      const r = rel ? join(rel, name) : name
      const abs = join(base, r)
      if (statSync(abs).isDirectory()) walk(base, r)
      else out[r] = readFileSync(abs, 'utf8')
    }
  }
  walk(dir, '')
  return out
}

function runFlow(sessionText: string, memoryDir: string) {
  const logs: string[] = []
  const res: any = getClearFlow()({ sessionText, memoryDir, log: (l: string) => logs.push(l) })
  const receipt: string = String(res?.receipt ?? res?.message ?? res?.reply ?? res?.text ?? '')
  return { res, logs, receipt }
}

test('H24[复现]_确实无轮次_不写文件_旧摘要逐字保留_回执无失败提示', () => {
  const { dir, snap } = seedMemory()
  const session = userLine('') + '\n' + asstLine({ type: 'tool_use', name: 'Bash', input: {} })
  const { logs, receipt } = runFlow(session, dir)
  expect(snapshot(dir)).toEqual(snap)                       // 一个字节都不动
  expect(countEvent(logs, 'clear_summary_empty_skip')).toBeGreaterThanOrEqual(1)
  expect(receipt).not.toMatch(/没能生成摘要|未生成摘要|失败/)  // 无失败提示
})

test('H24[复现]_有轮次但提取为空_不写文件_旧摘要逐字保留', () => {
  const { dir, snap } = seedMemory()
  // 有用户轮次，但助手侧没有可提取的"发给用户的话"（只有内部 text 块）
  const session = channelUserLine('FAKECHAT-1', '在吗') + '\n' + asstLine(txtBlock('内部草稿'))
  const { logs } = runFlow(session, dir)
  expect(snapshot(dir)).toEqual(snap)
  expect(countEvent(logs, 'clear_summary_extract_empty')).toBeGreaterThanOrEqual(1)
  expect(logs.some(l => /clear_summary_extract_empty\s+turns=\d+/.test(l))).toBe(true)
})

test('H24[复现]_有轮次但提取为空_回执含用户可见的失败提示', () => {
  const { dir } = seedMemory()
  const session = channelUserLine('FAKECHAT-1', '在吗') + '\n' + asstLine(txtBlock('内部草稿'))
  const { receipt } = runFlow(session, dir)
  expect(receipt).toMatch(/没能生成摘要|未生成摘要/)           // 明确告诉用户没存上
})

test('H24[复现]_提取非空_照常覆盖旧摘要（既有行为）', () => {
  const { dir } = seedMemory()
  const session = channelUserLine('FAKECHAT-1', '周末去哪玩') + '\n' + asstLine(replyBlock('去爬山呀'))
  const { receipt } = runFlow(session, dir)
  const after = snapshot(dir)
  const merged = Object.values(after).join('\n')
  expect(merged).toContain('去爬山呀')                       // 新摘要写进去了
  expect(merged).not.toContain('上一次的旧摘要内容')          // 旧摘要被覆盖
  expect(receipt).not.toMatch(/没能生成摘要|未生成摘要/)
})
