// 验收测试：pub-clear-sentinel
// 只通过 dispatcher/clear_summary.ts 的公开导出调用；run 用测试里自造的假函数。
import { describe, expect, test } from 'bun:test'
import { buildSummaryPrompt, mechanicalDigest, summarizeTurns } from '../../../dispatcher/clear_summary.ts'

type Turn = { role: 'user' | 'assistant'; text: string }

const TURNS: Turn[] = [
  { role: 'user', text: '明天中午一起吃饭吧' },
  { role: 'assistant', text: '好呀，那我们十二点在楼下见' },
]


// 对一组占位句变体逐个调用，返回 {输入 -> 输出}，失败时能直接看出哪个变体错了
async function runAll(variants: string[]): Promise<Record<string, string>> {
  const out: Record<string, string> = {}
  for (const v of variants) {
    out[v] = await summarizeTurns(TURNS, () => v, 0)
  }
  return out
}

function allEmpty(variants: string[]): Record<string, string> {
  const exp: Record<string, string> = {}
  for (const v of variants) exp[v] = ''
  return exp
}

describe('summarizeTurns 占位句识别', () => {
  test('行为1 run 返回标准占位句时返回空串而不是机械截取', async () => {
    expect(mechanicalDigest(TURNS).length).toBeGreaterThan(0)
    const r = await summarizeTurns(TURNS, () => '无实质内容。', 0)
    expect(r).toBe('')
  })

  test('行为2 无句号、前后空白换行、感叹号变体都返回空串', async () => {
    const variants = ['无实质内容', '  无实质内容。 \n', '无实质内容！']
    expect(await runAll(variants)).toEqual(allEmpty(variants))
  })

  test('行为3 列表符号或引号包装的占位句都返回空串', async () => {
    const variants = [
      '- 无实质内容。',
      '* 无实质内容',
      '1. 无实质内容。',
      '“无实质内容。”',
      '「无实质内容」',
      '- "无实质内容"。',
    ]
    expect(await runAll(variants)).toEqual(allEmpty(variants))
  })

  test('行为4a 要点在前占位句在后的多行文字返回非空且含要点', async () => {
    const r = await summarizeTurns(TURNS, () => '- 周六去看电影\n无实质内容。', 0)
    expect(r.length).toBeGreaterThan(0)
    expect(r).toContain('周六去看电影')
  })

  test('行为4b 占位句在第一行要点在第二行的多行文字返回非空且含要点', async () => {
    const r = await summarizeTurns(TURNS, () => '无实质内容。\n- 周六去看电影', 0)
    expect(r.length).toBeGreaterThan(0)
    expect(r).toContain('周六去看电影')
  })

  test('行为5 正常摘要原样保留要点', async () => {
    const r = await summarizeTurns(TURNS, () => '- 明天 10 点一起去超市', 0)
    expect(r).toContain('明天 10 点一起去超市')
  })

  test('行为6a run 返回 null 时返回机械截取结果', async () => {
    const r = await summarizeTurns(TURNS, () => null, 0)
    expect(r).toBe(mechanicalDigest(TURNS))
    expect(r.length).toBeGreaterThan(0)
  })

  test('行为6b run 抛异常时返回机械截取结果', async () => {
    const r = await summarizeTurns(
      TURNS,
      () => {
        throw new Error('boom')
      },
      0,
    )
    expect(r).toBe(mechanicalDigest(TURNS))
    expect(r.length).toBeGreaterThan(0)
  })

  test('行为7 turns 为空数组时返回空串且不调用 run', async () => {
    let called = 0
    const r = await summarizeTurns(
      [],
      () => {
        called++
        return '- 不该出现'
      },
      0,
    )
    expect(r).toBe('')
    expect(called).toBe(0)
  })
})

describe('buildSummaryPrompt 提示词内容', () => {
  const prompt = buildSummaryPrompt(TURNS)

  test('行为8 要求记下正在进行的情景，含地点/在做什么/关系，中性措辞不复述露骨细节，寒暄不算', () => {
    const required = ['正在进行', '地点', '在做什么', '关系', '中性', '露骨', '寒暄']
    const missing = required.filter((w) => !prompt.includes(w))
    expect(missing).toEqual([])
  })

  test('行为9 仍保留无内容只输出一行的句子，且限定为没有计划约定也没有正在进行的情景时才这样输出', () => {
    expect(prompt).toContain('没有值得记的内容就只输出一行：无实质内容。')
    expect(prompt).toContain('正在进行的情景时才')
  })

  test('行为10 正在进行的情景也写一行 排在 没有值得记的内容就只输出一行 之前', () => {
    const scene = prompt.indexOf('正在进行的情景也写一行')
    const none = prompt.indexOf('没有值得记的内容就只输出一行')
    expect(scene).toBeGreaterThanOrEqual(0)
    expect(none).toBeGreaterThanOrEqual(0)
    expect(scene).toBeLessThan(none)
  })
})

