/**
 * provider 指纹纯函数模块（Bug1 —— provider 指纹误报重启）。
 *
 * 背景（事故）：dispatcher 原先在每条消息到达时读【全局】`~/.claude/settings.json`
 * 的 env，取 BASE_URL/MODEL/token/API_KEY 拼串算指纹。但 worker 实际生效的 provider
 * 来自【该 bot 项目级】`<bot_dir>/.claude/settings.json`（CLI 用 `--setting-sources
 * user,project,local` 三档加载、项目级覆盖全局级）。源不同 + 字段多（token 轮换会改指纹）
 * → 三个 bot 的 provider 明明没变却每条消息都报"provider 变了 → 重启 worker"，
 * 把正在跑的 worker 杀掉重生，用户侧表现为"对话突然中断"。
 *
 * 修法（本模块）：数据源改为读该 bot 的项目级 settings（键缺失时逐键回退全局），
 * 指纹字段**只留 BASE_URL + MODEL**（token/API_KEY/DEFAULT_*_MODEL 一律不参与——
 * token 轮换与 provider 无关，绝不应触发重启）。
 *
 * 分层：`mergeProviderEnv` / `computeProviderFp` 是**零 IO 纯函数**（可直接测）；
 * `readProviderFp` 是薄 IO 外壳（读文件依赖可注入，测试传假读函数与假内容）。
 * 所有导出函数**永不抛**（错误分类器/指纹误抛会把主流程搞崩，比不判定更糟）。
 *
 * 硬约束（安全）：本模块只提取/拼入 BASE_URL/MODEL；对 ANTHROPIC_AUTH_TOKEN /
 * ANTHROPIC_API_KEY 等**绝不提取、绝不拼入、绝不记录**。指纹是 sha256 摘要（非机密）。
 */
import { readFileSync } from 'fs'
import { createHash } from 'node:crypto'

// ─── 关联实现（改算法需同步评估）──────────────────────────────────────
// 私有版曾有一份**同类但已分叉**的指纹实现：同为 sha256(BASE_URL|MODEL) 前 16 位，
// 但读【全局】settings、落在 <jsonl>.providerfp，用途是"指纹变化 → 一次性剥离旧 thinking"（不杀
// worker）。区别：本模块读项目级三档逐键合并、用于 dispatcher 决定是否杀 worker。
// 两者是同一概念的重复实现——若改本模块的公式/字段/读源，
// 需评估那处是否同步，以免两份实现静默分叉。

// ─── 类型 ────────────────────────────────────────────────────────────

/** worker 实际生效的 provider 身份（仅这两个字段参与指纹）。 */
export interface ProviderEnv {
  baseUrl: string   // ANTHROPIC_BASE_URL，缺失为 ''
  model: string     // ANTHROPIC_MODEL，缺失为 ''
}

// readProviderFp 的返回态：
//   string → 有效指纹（16 位 hex，**"全空"也是确定值**，参与比较）
//   null   → 不可判定（某份读源读失败/解析失败），调用方【不得】据此判"变了"
//   永不抛

// ─── 常量 ────────────────────────────────────────────────────────────

const BASE_URL_KEY = 'ANTHROPIC_BASE_URL'
const MODEL_KEY = 'ANTHROPIC_MODEL'

// ─── 合并（纯函数）────────────────────────────────────────────────────

/**
 * 纯函数：把已解析的 env 按档位逐键合并（低 → 高优先级，高者覆盖同名键）。
 * 只取 ANTHROPIC_BASE_URL / ANTHROPIC_MODEL 两个键；其它键一律忽略。
 * 参数可以为 null/undefined（当空对象处理）。永不抛。
 *
 * 档位顺序（低→高）：globalShared < globalLocal < projectShared < projectLocal。
 *
 * 值类型约定：只有当对应键的值为 string 时才覆盖；非 string（对象/数字/布尔）
 * 一律视作"该键未提供"，沿用低档值——避免把脏值转成 "[object Object]" 污染指纹。
 */
export function mergeProviderEnv(
  globalSharedEnv: Record<string, unknown> | null | undefined,
  globalLocalEnv: Record<string, unknown> | null | undefined,
  projectSharedEnv: Record<string, unknown> | null | undefined,
  projectLocalEnv: Record<string, unknown> | null | undefined,
): ProviderEnv {
  let baseUrl = ''
  let model = ''
  for (const layer of [globalSharedEnv, globalLocalEnv, projectSharedEnv, projectLocalEnv]) {
    if (!layer || typeof layer !== 'object') continue
    const b = (layer as Record<string, unknown>)[BASE_URL_KEY]
    if (typeof b === 'string') baseUrl = b
    const m = (layer as Record<string, unknown>)[MODEL_KEY]
    if (typeof m === 'string') model = m
  }
  return { baseUrl, model }
}

// ─── 指纹（纯函数）────────────────────────────────────────────────────

/**
 * 纯函数：由 baseUrl + model 拼串 "baseUrl|model" 算 sha256，取前 16 位 hex。
 * **恒返回确定指纹**（"两键皆空"也是确定值 sha256("|")——全空配置的"有→无/无→有"
 * 切换必须参与比较，否则会静默漏检；null 只保留给"读/解析失败"的不可判定态，
 * 见 readProviderFpStatus）。永不抛。
 */
export function computeProviderFp(env: ProviderEnv): string {
  const baseUrl = typeof env?.baseUrl === 'string' ? env.baseUrl : ''
  const model = typeof env?.model === 'string' ? env.model : ''
  return createHash('sha256').update(`${baseUrl}|${model}`).digest('hex').slice(0, 16)
}

// ─── 薄 IO（读四份 settings）──────────────────────────────────────────

/** 单份读源的判定结果：对象 = 可用 env；null = 不可判定（该份读失败/解析失败）。 */
function __readEnvLayer(path: string, readFile: (path: string) => string): Record<string, unknown> | null {
  let raw: string
  try {
    raw = readFile(path)
  } catch (err) {
    // 文件不存在 → 合法，该份视作空 env，继续下一档。
    // 其它读失败（EACCES/EISDIR…）→ 不可判定：在 provider_config 写入瞬间读到半截
    // 文件若当空处理，会把整串回退到低档 → 指纹错切 → 假重启（正是本次要修的 bug 换了触发点）。
    if ((err as { code?: unknown } | null | undefined)?.code === 'ENOENT') return {}
    return null
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null   // 半截文件 / 空文件 → 解析失败 → 不可判定
  }
  // 顶层非对象（null/数组/基本类型）→ 视作空，不算失败，继续合并（不抛 TypeError）。
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
  const env = (parsed as Record<string, unknown>).env
  // env 键不是对象（null/数组/基本类型）→ 该份视作空，继续。
  if (!env || typeof env !== 'object' || Array.isArray(env)) return {}
  return env as Record<string, unknown>
}

/** readProviderFp / readProviderFpStatus 的入参。 */
type ReadProviderFpOpts = {
  globalSharedPath: string
  globalLocalPath: string
  projectSharedPath: string
  projectLocalPath: string
  readFile?: (path: string) => string    // 默认 (p)=>readFileSync(p,'utf8')；测试注入假读
}

/** readProviderFpStatus 的返回态。 */
export interface ProviderFpStatus {
  /** 16 位 hex 指纹 | null（唯一来路：某份读失败/解析失败，不可判定）。 */
  fp: string | null
  /** true = 某份读失败/解析失败（不可判定，应告警）；false = 合法（含"四份皆无 provider 键"，此时 fp 为全空确定指纹）。 */
  uncertain: boolean
}

/**
 * 薄 IO：读四份 settings.json（按档位低→高）→ 逐键合并 → 算指纹，并区分 null 的两条来路，
 * 供 dispatcher 决定是否告警（"四份皆无 provider 键"是合法配置，不该刷"故障"告警）。
 *
 * 四份（优先级低→高）：
 *   globalShared  ~/.claude/settings.json
 *   globalLocal   ~/.claude/settings.local.json
 *   projectShared <bot_dir>/.claude/settings.json
 *   projectLocal  <bot_dir>/.claude/settings.local.json
 *
 * 逐份读取契约（保证永不抛）：
 *   - ENOENT（不存在）           → 该份视作 {}，继续下一档
 *   - 其它读失败(EACCES/EISDIR…) → uncertain=true（不可判定，防误判）
 *   - 读成功但 JSON.parse 失败   → uncertain=true（不可判定，防半截文件）
 *   - 解析成功但顶层非对象(*)    → 该份视作 {}（不抛 TypeError，继续）
 *       (*) 顶层为 null / 数组 / 基本类型，或 env 键不是对象
 * 四份处理完 → mergeProviderEnv → computeProviderFp（**恒出确定指纹**；"四份皆无 provider 键"
 * 是合法配置，给出全空确定指纹 sha256("|")，让"有→无/无→有"切换能被上层检出）。
 *
 * @returns { fp: 16 位 hex | null, uncertain: 该 null 是否属"读/解析失败" }
 * 永不抛。
 */
export function readProviderFpStatus(opts: ReadProviderFpOpts): ProviderFpStatus {
  const readFile = opts.readFile ?? ((p: string) => readFileSync(p, 'utf8'))
  const layers: Record<string, unknown>[] = []
  for (const path of [opts.globalSharedPath, opts.globalLocalPath, opts.projectSharedPath, opts.projectLocalPath]) {
    const env = __readEnvLayer(path, readFile)
    if (env === null) return { fp: null, uncertain: true }   // 不可判定：不继续合并、不回退
    layers.push(env)
  }
  const fp = computeProviderFp(mergeProviderEnv(layers[0], layers[1], layers[2], layers[3]))
  return { fp, uncertain: false }   // fp 恒为确定指纹（含"四份皆无 provider 键"的全空值）
}

/**
 * readProviderFpStatus 的薄包装，只取指纹（保持原对外签名不变）。永不抛。
 * @returns 16 位 hex 指纹（含全空确定值）| null（唯一来路：不可判定，调用方不得据此判"变了"）
 */
export function readProviderFp(opts: ReadProviderFpOpts): string | null {
  return readProviderFpStatus(opts).fp
}
