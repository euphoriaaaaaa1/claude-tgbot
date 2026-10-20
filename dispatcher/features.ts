/**
 * 每只 bot 的功能开关读取（bot-features 第一批 F1-1；INTERFACE-bot-features.md 3.0 第 2、3 条与 3.8.1）。
 *
 * 开关存在频道目录的 features.json，唯一写者是本仓的 bot_features.py；
 * 调度器、worker 插件只经这里读，读法与 Python 的 bot_features.read 逐项一致：
 * - 文件不存在：八项都开（没初始化的现有 bot），readable 为 true；
 * - 文件在、顶层是对象：值是布尔的键按值；缺的键取 default，default 缺或不是布尔按关；
 *   值不是布尔的键按关，并加一条警告；
 * - 读不出或顶层不是对象：八项都关，warnings 为空，readable 为 false。
 *   "读不出"含：不是普通文件、超过 64 KiB、读文件出错、不是合法 UTF-8（含开头带 BOM）、不是合法 JSON。
 * 按（修改时间, 大小, inode）缓存，文件一改下一次读就是新值，不用重启进程。
 * 导入没有副作用：不读文件、不读环境变量、不起定时器。
 */
import { readFileSync, statSync, type Stats } from 'fs'
import { join } from 'path'

/**
 * 八个功能键与中文名，顺序即 INTERFACE 3.0 第 2 条（页面块顺序）。
 * 键的唯一定义在 Python 的 bot_features.FEATURES，这里是 TS 侧副本，由 tests/unit/features_consistency.test.ts 核对。
 */
export const FEATURE_LABELS = Object.freeze({
  voice: '发语音',
  image: '生图',
  stickers: '表情包',
  moments: '朋友圈',
  proactive: '主动找你聊',
  life: '生活作息',
  relationship: '关系进展',
  groups: '群聊',
} as const)

export type FeatureKey = keyof typeof FEATURE_LABELS
export type FeatureFlags = Record<FeatureKey, boolean>
export type FeaturesRead = { features: FeatureFlags; warnings: string[]; readable: boolean }

/** 八个键，顺序同 FEATURE_LABELS。 */
export const FEATURE_KEYS: readonly FeatureKey[] = Object.freeze(Object.keys(FEATURE_LABELS) as FeatureKey[])

const FILE_NAME = 'features.json'

// 超过这个大小按读不出处理：避免超大文件整份读进内存、阻塞事件循环。恰好等于上限仍正常读取。
const MAX_FILE_BYTES = 64 * 1024

// fatal：不是合法 UTF-8 就抛错，按读不出处理；ignoreBOM：开头的 BOM 留在文本里，让 JSON.parse 失败。
// 两条都是为了与 Python 按 utf-8 读成文本再 json.loads 的结果相同。
const UTF8 = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })

type CacheEntry = { mtimeMs: number; size: number; ino: number; result: FeaturesRead }
const cache = new Map<string, CacheEntry>()

const isObject = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v)

function allAs(value: boolean): FeatureFlags {
  const out = {} as FeatureFlags
  for (const k of FEATURE_KEYS) out[k] = value
  return out
}

const unreadable = (): FeaturesRead => ({ features: allAs(false), warnings: [], readable: false })

// 缓存里的对象不交给调用方，免得调用方改了返回值连带改坏缓存
const copy = (r: FeaturesRead): FeaturesRead => ({ features: { ...r.features }, warnings: [...r.warnings], readable: r.readable })

/** 由文件原文算出结果（3.0 第 3 条）。 */
function interpret(text: string): FeaturesRead {
  let doc: unknown
  try { doc = JSON.parse(text) } catch { return unreadable() }
  if (!isObject(doc)) return unreadable()
  const features = allAs(doc.default === true)
  // features 不是对象时等于八个键都没写，全部取 default
  const given = isObject(doc.features) ? doc.features : {}
  const warnings: string[] = []
  for (const k of FEATURE_KEYS) {
    if (!Object.hasOwn(given, k)) continue
    const v = given[k]
    if (typeof v === 'boolean') {
      features[k] = v
    } else {
      features[k] = false
      warnings.push(`${FEATURE_LABELS[k]}的开关值不是真假，已按关着处理`)
    }
  }
  return { features, warnings, readable: true }
}

/** 读 <channelDir>/features.json；文件是什么状态都不抛错，读不出一律按八项全关。只读，不建文件。 */
export function readFeatures(channelDir: string): FeaturesRead {
  const path = join(channelDir, FILE_NAME)
  let st: Stats
  try {
    st = statSync(path)
  } catch (e) {
    cache.delete(path)
    // 只有确定不存在才按没初始化的全开；没权限、路径中间不是目录等一律按读不出全关
    if ((e as { code?: unknown } | null)?.code === 'ENOENT') return { features: allAs(true), warnings: [], readable: true }
    return unreadable()
  }
  // 目录、FIFO 等按读不出：没有写端的 FIFO 一读就会把调用方整个卡住
  if (!st.isFile()) {
    cache.delete(path)
    return unreadable()
  }
  const hit = cache.get(path)
  if (hit && hit.mtimeMs === st.mtimeMs && hit.size === st.size && hit.ino === st.ino) return copy(hit.result)
  // 超限文件不读内容；结论与文件内容无关，可以直接进缓存（stat 三项变化后会重新判定）
  if (st.size > MAX_FILE_BYTES) {
    const result = unreadable()
    cache.set(path, { mtimeMs: st.mtimeMs, size: st.size, ino: st.ino, result })
    return copy(result)
  }
  let raw: Uint8Array
  try {
    raw = readFileSync(path)
  } catch {
    // 读失败可能是一时的（例如权限改回来时修改时间与大小都不变），不进缓存
    cache.delete(path)
    return unreadable()
  }
  let result: FeaturesRead
  try { result = interpret(UTF8.decode(raw)) } catch { result = unreadable() }
  cache.set(path, { mtimeMs: st.mtimeMs, size: st.size, ino: st.ino, result })
  return copy(result)
}

/** 这一项开着吗：只有值恰为 true 才算开，缺的、不是布尔的一律按关（与读不出按全关同向）。 */
export function featureOn(features: FeatureFlags, key: FeatureKey): boolean {
  return features?.[key] === true
}

/** 关着的功能键，顺序同 3.0 第 2 条；要中文名用 FEATURE_LABELS[键]。 */
export function offList(features: FeatureFlags): FeatureKey[] {
  return FEATURE_KEYS.filter(k => !featureOn(features, k))
}
