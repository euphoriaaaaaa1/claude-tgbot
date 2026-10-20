/**
 * H-27 · `<channel>` 包装的注入转义 + 内部消息的元数据判定。
 *
 * 用户正文会被原样拼进 `<channel ...>...\n正文\n</channel>` 包装里；正文里若出现
 * `<channel` / `</channel` 会破坏包装结构（伪造出开闭标签）。这里只把这**两个前缀**
 * 转义成 `&lt;channel` / `&lt;/channel`，其余字符逐字保留；并提供可逆操作，方便断言
 * "转义 → 逆操作 = 原文"。
 *
 * 内部消息（synthetic / director / self-initiate 等）改看**元数据**（synthetic 标记），
 * 不再靠正文前缀判定——用户自己打 `[director]` 不会被误判为内部。
 */

/** 每次转义命中的序列数记一行（n=0 不记，避免每条消息都刷屏）。 */
const EVENT = 'channel_wrap_escaped'

const CLOSE_RE = /<\/channel/gi
const OPEN_RE = /<channel/gi

/**
 * 转义正文里的两个包装标记前缀。命中数 n>0 时向 log（缺省 stderr）记 `channel_wrap_escaped n=<次数>`。
 * 大小写不敏感；只替换前缀，被替换部分其余字符逐字保留。
 */
export function escapeChannelWrapper(text: string, log?: (line: string) => void): string {
  if (typeof text !== 'string' || !text) return typeof text === 'string' ? text : ''
  const n = (text.match(CLOSE_RE)?.length ?? 0) + (text.match(OPEN_RE)?.length ?? 0)
  if (n === 0) return text
  // 先转义带斜杠的闭合前缀，再转义开前缀：两步互不重叠（'</channel' 里不含 '<channel'）。
  const out = text.replace(CLOSE_RE, '&lt;/channel').replace(OPEN_RE, '&lt;channel')
  const sink = typeof log === 'function' ? log : (l: string) => process.stderr.write(`${l}\n`)
  sink(`${EVENT} n=${n}`)
  return out
}

/**
 * escapeChannelWrapper 的逆操作：把 `&lt;` 实体还原成 `<`，仅当前面紧跟 `/channel` 或 `channel`
 * （大小写不敏感）时；与转义配对可逐字还原原文。
 */
export function unescapeChannelWrapper(text: string): string {
  if (typeof text !== 'string' || !text) return typeof text === 'string' ? text : ''
  return text.replace(/&lt;(?=\/?channel)/gi, '<')
}

/** 内部消息判定：只看元数据 synthetic 标记，不看正文（H-27）。 */
export function isInternalMessage(meta: unknown): boolean {
  return !!meta && typeof meta === 'object' && !Array.isArray(meta)
    && (meta as Record<string, unknown>).synthetic === true
}
