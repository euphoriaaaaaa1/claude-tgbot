/**
 * 入站消息的时间标注（P3）：让 bot 知道"现在几点"和"距上次说话隔了多久"。
 *
 * 背景：入站消息只带 UTC 机器时间戳，模型会顺着上下文惯性以为还是早上。这里生成
 * 一段人类可读的中文前缀，拼在消息正文最前面（在 <channel> 标签内部，不动 meta 属性）。
 *
 * 纯函数：时区/语言/阈值全部参数注入，不取系统时钟、不读 env。任何非法输入都不抛。
 */
export type TimeOpts = { gapThresholdMin?: number; timeZone?: string; locale?: string }

const DEFAULT_GAP_MIN = 30
/** 时段词按本地小时切：0-5 凌晨 / 5-8 早上 / 8-11 上午 / 11-13 中午 / 13-18 下午 / 18-23 晚上 / 23-24 深夜 */
const PERIODS: [number, string][] = [
  [5, '凌晨'], [8, '早上'], [11, '上午'], [13, '中午'], [18, '下午'], [23, '晚上'], [24, '深夜'],
]

function periodWord(hour: number): string {
  for (const [end, word] of PERIODS) if (hour < end) return word
  return '深夜'
}

function formatParts(tsMs: number, locale: string, timeZone?: string): Record<string, string> | null {
  const base: Intl.DateTimeFormatOptions = {
    month: 'numeric', day: 'numeric', weekday: 'short',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }
  const build = (o: Intl.DateTimeFormatOptions) => {
    const out: Record<string, string> = {}
    for (const p of new Intl.DateTimeFormat(locale, o).formatToParts(new Date(tsMs))) out[p.type] = p.value
    return out
  }
  try { return build(timeZone ? { ...base, timeZone } : base) }
  catch { try { return build(base) } catch { return null } }   // 时区名非法 → 退回运行环境时区，绝不抛
}

/** 间隔措辞：<90 分钟按分钟，<24 小时按小时，否则按天（四舍五入） */
function gapPhrase(gapMs: number): string {
  const min = gapMs / 60_000
  if (min < 90) return `约 ${Math.round(min)} 分钟`
  if (min < 1440) return `约 ${Math.round(min / 60)} 小时`
  return `约 ${Math.round(min / 1440)} 天`
}

export function buildTimePrefix(tsMs: number, prevTsMs: number | null, opts?: TimeOpts): string {
  if (!Number.isFinite(tsMs) || tsMs <= 0) return ''
  const locale = typeof opts?.locale === 'string' && opts.locale ? opts.locale : 'zh-CN'
  const p = formatParts(tsMs, locale, typeof opts?.timeZone === 'string' && opts.timeZone ? opts.timeZone : undefined)
  if (!p) return ''

  const hour = Number(p.hour)
  const period = Number.isFinite(hour) ? periodWord(hour) : ''
  let out = `（现在：${p.month}/${p.day} ${p.weekday ?? ''} ${period}${p.hour}:${p.minute}）\n`

  const rawGap = opts?.gapThresholdMin
  const thresholdMin = typeof rawGap === 'number' && Number.isFinite(rawGap) && rawGap > 0 ? rawGap : DEFAULT_GAP_MIN
  const prev = prevTsMs
  // prev 缺失 / 坏值 / 时钟倒流（prev > ts）→ 只给时刻行，绝不出现"距上次 -3 小时"
  if (typeof prev === 'number' && Number.isFinite(prev) && prev > 0 && prev <= tsMs) {
    const gapMs = tsMs - prev
    if (gapMs > thresholdMin * 60_000) out += `【距你们上次说话已过去${gapPhrase(gapMs)}】\n`
  }
  return out
}
