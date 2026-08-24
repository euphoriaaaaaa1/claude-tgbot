/**
 * 把 reply 的 text 按 [[图N]] 标记拆成发送计划，让图片能插在文字气泡中间。
 *
 * 规则：
 * - 标记写法 [[图]] / [[图1]] / [[图2]] / [[图片2]]（数字 = files 数组里的第几张，从 1 数）
 * - 不带数字的 [[图]] 按出现顺序取下一张还没被点名的图
 * - 没被任何标记点名的图，维持旧行为：全部压在最后
 * - 指到不存在的张数、或重复点名同一张：标记删掉、不发（绝不把标记原文漏给用户）
 * - cleanText = 去掉全部标记后的文本，给 peer 转发等"要原文"的地方用
 *
 * 纯函数，不碰网络不碰文件，测试直接 import。
 */
export type PlanItem =
  | { kind: 'text'; text: string }
  | { kind: 'file'; index: number }

const MARKER_RE = /\[\[图(?:片)?\s*(\d*)\s*\]\]/g

export function buildSendPlan(text: string, fileCount: number): { plan: PlanItem[]; cleanText: string } {
  const plan: PlanItem[] = []
  const used = new Set<number>()
  let cursor = 0        // 无数字标记的顺序指针
  let last = 0

  for (const m of text.matchAll(MARKER_RE)) {
    const before = text.slice(last, m.index)
    if (before.trim()) plan.push({ kind: 'text', text: before })
    last = m.index! + m[0].length

    let idx: number
    if (m[1]) {
      idx = parseInt(m[1], 10) - 1
    } else {
      while (used.has(cursor)) cursor++
      idx = cursor
    }
    if (idx >= 0 && idx < fileCount && !used.has(idx)) {
      used.add(idx)
      plan.push({ kind: 'file', index: idx })
    }
    // 越界/重复：标记吞掉即可，什么都不发
  }
  const tail = text.slice(last)
  if (tail.trim()) plan.push({ kind: 'text', text: tail })

  // 没被点名的图维持旧行为：按原顺序压尾
  for (let i = 0; i < fileCount; i++) {
    if (!used.has(i)) plan.push({ kind: 'file', index: i })
  }

  return { plan, cleanText: text.replace(MARKER_RE, '').replace(/\n{3,}/g, '\n\n').trim() }
}
