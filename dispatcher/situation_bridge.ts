/**
 * 作息桥：问一次 python，拿回"她此刻在做什么"（学期 / 活动名 / 状态 / 这档能不能被打断）。
 *
 * 数据来自仓根的 hang_situation.py —— 只读 configs/<bot>.yml 的作息表，走的是
 * generators.situation 那一套判定，学期/节假日口径与主动消息完全一致。
 * 两个消费方共用这一份结果与缓存：被晾感知（该不该追问）和入站消息的情境锚点行。
 *
 * 桥挂 / 超时 / 输出不认识 → 一律 null，调用方降级为"这一轮什么都不做"，绝不崩、绝不瞎猜。
 * 日志只有 bot 标识与原因码，不带配置内容。
 */
import { spawnSync } from 'child_process'
import { join } from 'path'
import { platform } from 'os'
import type { Situation } from './hang_runtime'
import { formatSituLine } from './situ_anchor'

const REPO_ROOT = join(import.meta.dir, '..')
const SITU_PY = join(REPO_ROOT, 'hang_situation.py')
const PROBE_TIMEOUT_MS = 3000
const TTL_MS = 5 * 60_000        // 作息是小时级粒度，5 分钟内复用够新
const WARN_MS = 10 * 60_000      // 桥挂时日志最多这么频，别刷屏

// Windows 官方安装器只装 python.exe（没有 python3.exe），取用顺序与
// windows/register-tasks.ps1 一致；装在 PATH 之外的用 PYTHON_BIN 指过去。
export const PYTHON_BIN = process.env.PYTHON_BIN
  || (platform() === 'win32' ? 'python' : 'python3')

/** hang 只认前三个字段；term_label 是给锚点行用的后加字段，结构上向后兼容 */
export type SituationInfo = Situation & { termLabel: string }

let cache: { atMs: number; sit: SituationInfo | null } | null = null
let warnedAtMs = 0

/**
 * 她此刻在做什么；查不到返回 null。
 * ponytail: 失败也进缓存——桥真挂了就别每条消息都去 spawn 一个注定超时 3 秒的 python。
 * 代价是桥恢复后最多晚 5 分钟才被发现，作息粒度下无所谓。
 */
export function probeSituation(bot: string, nowMs: number = Date.now()): SituationInfo | null {
  if (cache && nowMs - cache.atMs < TTL_MS) return cache.sit
  let sit: SituationInfo | null = null
  try {
    const r = spawnSync(PYTHON_BIN, [SITU_PY, bot], {
      cwd: REPO_ROOT, encoding: 'utf8', timeout: PROBE_TIMEOUT_MS,
    })
    if (r.status !== 0 || typeof r.stdout !== 'string' || !r.stdout.trim()) {
      throw new Error('bridge_unavailable')
    }
    const o = JSON.parse(r.stdout)
    if (!o || typeof o.name !== 'string' || typeof o.state !== 'string') {
      throw new Error('bad_shape')
    }
    sit = {
      name: o.name, state: o.state, interruptible: o.interruptible !== false,
      termLabel: typeof o.term_label === 'string' ? o.term_label : '',
    }
  } catch {
    if (nowMs - warnedAtMs > WARN_MS) {
      warnedAtMs = nowMs
      process.stderr.write(`situation-bridge: probe_failed bot=${bot}\n`)
    }
  }
  cache = { atMs: nowMs, sit }
  return sit
}

/** 入站消息前的情境锚点行；桥挂 → 空串（这条不带锚点，绝不挡投递）。 */
export function situAnchorLine(bot: string, nowMs: number = Date.now()): string {
  const sit = probeSituation(bot, nowMs)
  return sit ? formatSituLine(sit.termLabel, sit.name) : ''
}
