/**
 * provider_sync.ts — settings.json 的 provider 签名 + 刷新/防抖判据。
 *
 * 纯函数，不碰网络不碰文件，测试直接 import（同 send_plan.ts 先例）。
 * IO 那一侧（fs.watch、轮询兜底、真重启）在 provider_watch.ts。
 * 契约见 .devflow/INTERFACE-provider-sync.md。
 */
import { createHash } from 'crypto'

// 参与签名的 provider 字段：env 里四个 + 顶层 model。改这张表 = 改「什么算换了 provider」。
const ENV_FIELDS = [
  'ANTHROPIC_BASE_URL',
  'ANTHROPIC_AUTH_TOKEN',
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_MODEL',
] as const

// 每个字段单独摘要再拼接：任一字段变 → 签名必变，且签名里不留任何字段明文（含 token）。
// 缺失/非字符串按空串参与，所以「缺字段」与「字段是空串」签名相同。
function digest(v: unknown): string {
  const s = typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v)
  return createHash('sha256').update(s).digest('hex').slice(0, 16)
}

export function providerSig(settingsJsonText: string): string {
  if (typeof settingsJsonText !== 'string') {
    throw new TypeError('providerSig: settingsJsonText must be a string')
  }
  if (!settingsJsonText.trim()) return ''
  let parsed: unknown
  try { parsed = JSON.parse(settingsJsonText) } catch { return '' }
  // 数组在 JS 里也是 object，但 settings.json 只能是普通对象；数组/null/标量一律算「读不出配置」
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return ''
  const top = parsed as Record<string, unknown>
  const env = (top.env && typeof top.env === 'object' && !Array.isArray(top.env))
    ? top.env as Record<string, unknown>
    : {}
  // 非空串保证「配置存在但五字段全空」能与「读不出配置」('')区分开
  return [...ENV_FIELDS.map(k => digest(env[k])), digest(top.model)].join('.')
}

export function shouldRefresh(prevSig: string, nextSig: string): boolean {
  if (typeof prevSig !== 'string' || typeof nextSig !== 'string') {
    throw new TypeError('shouldRefresh: prevSig/nextSig must be strings')
  }
  // 空签名只代表「读不出」：既不当基线也不当新值，避免文件被改写到一半时白重启
  return prevSig !== '' && nextSig !== '' && prevSig !== nextSig
}

export function debounceGate(lastMs: number, nowMs: number, windowMs: number): boolean {
  if (!Number.isFinite(lastMs) || !Number.isFinite(nowMs) || !Number.isFinite(windowMs)) {
    throw new TypeError('debounceGate: lastMs/nowMs/windowMs must be finite numbers')
  }
  // 恰好等于窗口即生效；窗口 <= 0 = 不防抖；时钟回拨(now<last) 差值为负 → 压掉
  return nowMs - lastMs >= Math.max(0, windowMs)
}
