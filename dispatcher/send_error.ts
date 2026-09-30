/**
 * 发送错误分类纯函数模块（Bug2 —— Telegram 发送失败即丢消息）。
 *
 * 背景（事故）：dispatcher 的 sendChunk 在 sendMessage 失败时只对"确定未送达"重试，
 * 判据是 `e.cause.code` 匹配 /ECONNREFUSED|ENOTFOUND|.../。但实测(grammY v1.42.0 + bun)：
 *   - 底层网络错误挂在 `e.error`（HttpError），不是 `.cause`（grammY 从不设 `.cause`）→ 判据恒 undefined。
 *   - bun 的码是驼峰无 E 前缀（拒连=ConnectionRefused），正则 /ECONNREFUSED/i 匹配不上；
 *     本机 DNS 失败经代理/TUN 竟表现为 ECONNRESET。
 *   - 结果：网络错误全被判"可能已送达"→ 跳过不重试。这就是"发送失败即丢消息"。
 *
 * 修法（本模块）：三分类——
 *   retryable   确定未送达、可自动重试：GrammyError 429；HttpError 且底层码属"建连前阶段失败"
 *   undelivered 确定未送达、重试无益：GrammyError 4xx（非 429）
 *   ambiguous   可能已送达/状态未知——绝不重发：其余一切（ECONNRESET/超时/Abort/JSON解析/5xx/未识别）
 *
 * 唯一可靠原则：失败发生在 HTTP 请求体发出【之前】才算"确定未送达"。故建连前判据用
 * **精确字符串集合**（非子串正则、非负向清单）：不在集合内一律 ambiguous（默认安全方向）。
 * 这条是防"重发已送达的段→用户看到 A A B B"（实测事故）的最后一道闸，宁可少重试。
 *
 * 判据读取兼容 `e.error.code`（bun）与 `e.error.cause.code`（Node/undici）。
 *
 * 纯函数：零 IO、不读 env、不打印。错误对象由调用方传入，分类结果（含原因串）返回给
 * 调用方决定怎么记日志。三个函数**永不抛**（分类器抛错会把 catch 分支本身搞崩，比丢消息更糟）。
 *
 * 安全：reason 只含结构化字段（cls/code），**绝不含** String(e) 全文 / token / 用户文本 / 底层 message。
 */
import { GrammyError, HttpError } from 'grammy'

// ─── 类型 ────────────────────────────────────────────────────────────

export type SendClass =
  | 'retryable'    // 确定未送达，可自动重试（429 限流；建连前阶段失败）
  | 'undelivered'  // 确定未送达，但重试无益（Telegram 4xx 确定性拒收）
  | 'ambiguous'    // 可能已送达 / 状态未知 —— 绝不重发

export interface SendErrorInfo {
  cls: SendClass
  /** 归一化原因串，仅含结构化字段：如 'GrammyError 429' / 'code=ConnectionRefused' / 'unknown'。
   *  禁止含 token、禁止含用户文本、禁止是 String(e) 全文。 */
  reason: string
  /** 仅 429 存在：Telegram 返回的 retry_after（秒）。其余 undefined。 */
  retryAfterSec?: number
}

// ─── 建连前失败集合（精确匹配）─────────────────────────────────────────

// 只收录**已确证属"HTTP 请求体发出之前"**的失败码，两种拼写各自字面量入集合：
//   bun(驼峰) 与 Node/undici(全大写) 不折叠大小写。
// 不在集合内一律 false → 上层归 ambiguous。ECONNRESET/ETIMEDOUT/ECONNABORTED/AbortError
// 本就不在集合内、自然落 ambiguous，无需独立负向清单。
// 取舍记录(S3)：UND_ERR_CONNECT_TIMEOUT 实为建连超时(确定未送达)，但保守暂不纳入 → 归 ambiguous；
//   T5 真机确证后再收窄纳入。TLS 握手类真机码同样待 T5 逐条增补。
const PRE_CONNECT_CODES = new Set<string>([
  'ConnectionRefused',   // bun：TCP 建连被拒
  'ECONNREFUSED',        // Node/undici：同上
  'ENOTFOUND',           // DNS 解析失败(无此主机)
  'EAI_AGAIN',           // DNS 临时解析失败
  'ENETUNREACH',         // 网络不可达
  'EHOSTUNREACH',        // 主机不可达
])

/**
 * 纯函数：判断一个网络错误码是否属"HTTP 请求体发出之前"的失败（可安全重试）。
 * 实现：**精确字符串匹配**上述已确证集合（非子串、非正则、大小写不折叠）；
 *       不在集合内一律返回 false → 上层归 'ambiguous'（默认安全方向）。永不抛。
 */
export function isPreConnectFailure(code: string): boolean {
  return typeof code === 'string' && PRE_CONNECT_CODES.has(code)
}

// ─── 分类 ────────────────────────────────────────────────────────────

/**
 * 纯函数：把一次 sendMessage 抛出的错误分类。永不抛；无法识别一律 'ambiguous'。
 * 分流规则见模块头；reason 的精确字面量见 INTERFACE §2.4（逐字实现，勿自由发挥）。
 */
export function classifySendError(e: unknown): SendErrorInfo {
  try {
    if (e instanceof GrammyError) {
      const code = e.error_code
      const reason = `GrammyError ${code}`
      // 429 限流：确定未送达，可退避重试；携带 retry_after。
      if (code === 429) {
        const ra = e.parameters?.retry_after
        return typeof ra === 'number' ? { cls: 'retryable', reason, retryAfterSec: ra } : { cls: 'retryable', reason }
      }
      // 4xx（非 429）：Telegram 确定性拒收（400/401/403/404/413/499…），重试无益。
      if (typeof code === 'number' && code >= 400 && code < 500) {
        return { cls: 'undelivered', reason }
      }
      // 5xx 及其它数值：状态未知 → 绝不重发。
      return { cls: 'ambiguous', reason }
    }

    if (e instanceof HttpError) {
      // 双读：e.error.code（bun）/ e.error.cause.code（Node/undici）/ e.error.name（AbortError 等）。
      // 取码链严格按 INTERFACE §2.2；全程 ?. 保护，异常内层对象不抛。
      // 注：真机 abort 抛的是 DOMException，其 .code 是**数字**（AbortError=20）→ 此处会得到 "code=20"，
      // 与 INTERFACE §2.4 表的 "code=AbortError"（name 路径）不一致；但分类仍为 ambiguous（安全方向对），
      // 仅为日志差异，故不为此改动取码链（避免 Node 下 cause.code 取不到的风险）。
      const inner = e.error as
        | { code?: unknown; cause?: { code?: unknown } | null; name?: unknown }
        | null
        | undefined
      const code = String(inner?.code ?? inner?.cause?.code ?? inner?.name ?? '')
      const reason = code === '' ? 'HttpError' : `code=${code}`
      return { cls: isPreConnectFailure(code) ? 'retryable' : 'ambiguous', reason }
    }

    return { cls: 'ambiguous', reason: 'unknown' }
  } catch {
    return { cls: 'ambiguous', reason: 'unknown' }
  }
}

/**
 * 纯函数：把分类结果转成一行日志片段（只含 cls + reason，不含 token/用户文本）。永不抛。
 * 格式：`cls=<cls> reason=<reason>`，若 retryAfterSec != null 追加 ` retry_after=<n>s`。
 */
export function describeSendError(info: SendErrorInfo): string {
  try {
    const base = `cls=${info?.cls} reason=${info?.reason}`
    return info?.retryAfterSec != null ? `${base} retry_after=${info.retryAfterSec}s` : base
  } catch {
    return 'cls=ambiguous reason=unknown'
  }
}
