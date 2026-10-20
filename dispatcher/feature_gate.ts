/**
 * 调度器按功能开关下的闸里能单测的两块（bot-features 第一批 F1-4；INTERFACE-bot-features.md 3.4.3、3.8.5、3.8.7）。
 * dispatcher.ts 一导入就起 bot，可测逻辑照仓里惯例抽到独立模块。导入没有副作用：不读文件、不读环境变量。
 */
import { realpathSync } from 'fs'
import { basename, dirname, join, resolve, sep } from 'path'

// ─── DISPATCHER_TELEGRAM_API_BASE（3.8.5）────────────────────────────────
// Bot API 请求与文件下载都带着 bot 令牌，所以这个接缝只收回环地址：设成别的就拒绝启动，免得令牌发去别处。
const API_BASE_RE = /^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/

/**
 * 没设（undefined）→ `{ok: true, apiRoot: undefined}`，调用方照旧不传这个选项；
 * 设了就必须恰为 `http://127.0.0.1:<1-65535>`（不带结尾斜杠与路径，端口不带前导零）；空串也算设了、不合规。
 */
export function telegramApiBase(raw: string | undefined): { ok: true; apiRoot: string | undefined } | { ok: false } {
  if (raw === undefined) return { ok: true, apiRoot: undefined }
  if (!API_BASE_RE.test(raw)) return { ok: false }
  const port = Number(raw.slice(raw.lastIndexOf(':') + 1))
  return port <= 65535 ? { ok: true, apiRoot: raw } : { ok: false }
}

// ─── /send 的出口兜底：生图目录、表情库图片根目录（3.8.7 第 1 条）──────────────
export type DropKind = 'image' | 'stickers'
export type FileDrops = {
  /** 要丢掉的文件在请求 files 里的下标（[[图N]] 按下标对应，所以只跳过、不改数组）。 */
  dropped: Set<number>
  /** 丢掉的文件名（basename），按请求顺序。 */
  names: string[]
  counts: Record<DropKind, number>
}

/**
 * 先 realpath（软链、大小写都按磁盘上的真实路径；对原字符串做，`..` 按系统打开文件时的语义走）。
 * 路径不存在时取最深一层存在的上级目录的真实路径，再接上余下的部分：
 * 只退回字面路径的话，上级目录里有软链（例如 macOS 的 /var → /private/var）就和目录那边对不上。
 */
function real(p: string): string {
  try { return realpathSync(p) } catch { /* 不存在或读不了：往上找 */ }
  const abs = resolve(p)
  const parent = dirname(abs)
  return parent === abs ? abs : join(real(parent), basename(abs))
}

/** file 在 dir 之下（不含 dir 本身；dir 的同前缀兄弟目录不算）。 */
function under(file: string, dir: string): boolean {
  return file.startsWith(dir.endsWith(sep) ? dir : dir + sep)
}

/**
 * 算出这次 /send 要丢掉哪些文件：落在生图目录下而生图关着、或落在表情库图片根目录下而表情包关着。
 * 不是字符串或是空串的项不判（照旧交给发送那一步）。两项都开着时不碰文件系统。
 */
export function planFileDrops(
  files: readonly unknown[],
  dirs: { image: string; stickers: string },
  on: { image: boolean; stickers: boolean },
): FileDrops {
  const out: FileDrops = { dropped: new Set(), names: [], counts: { image: 0, stickers: 0 } }
  if (on.image && on.stickers) return out
  const imageDir = on.image ? '' : real(dirs.image)
  const stickerDir = on.stickers ? '' : real(dirs.stickers)
  files.forEach((f, i) => {
    if (typeof f !== 'string' || f === '') return
    const p = real(f)
    const kind: DropKind | null = imageDir && under(p, imageDir) ? 'image'
      : stickerDir && under(p, stickerDir) ? 'stickers' : null
    if (!kind) return
    out.dropped.add(i)
    out.names.push(basename(f))
    out.counts[kind]++
  })
  return out
}
