/**
 * worker 忙闲状态文件 <channel_dir>/.worker-state.json（INTERFACE-d07-restart-idle.md 一节）。
 *
 * worker 每轮回复开始时标记忙、放行/启动时按待投队列标记忙闲；重启前的
 * worker-idle-wait.sh 读它来决定要不要再等一会儿才杀。放在 channel_dir 根、
 * 不在 inbox 内——inbox 有 fs.watch 与 drainInbox，放进去会被当新消息。
 *
 * 文件格式：{"v":1,"busy":bool,"at":ms,"chat":str}；原子写（tmp + rename）。
 */
import { writeFileSync, renameSync } from 'fs'

export const WORKER_STATE_VERSION = 1

export type WorkerState = { v: number; busy: boolean; at: number; chat: string }

export function buildWorkerState(busy: boolean, chat: string, atMs: number): WorkerState {
  return { v: WORKER_STATE_VERSION, busy, at: atMs, chat }
}

/** 待投队列非空 → 仍算忙（放行/启动时用，r1 §3）。空队列 → 闲。 */
export function busyFromQueue(pendingCount: number): boolean {
  return pendingCount > 0
}

/** 原子写状态文件；任何 IO 失败静默吞掉，绝不因此影响 worker 主流程。 */
export function writeWorkerStateFile(
  path: string, busy: boolean, chat: string, atMs: number = Date.now(),
): void {
  try {
    const tmp = `${path}.tmp`
    writeFileSync(tmp, JSON.stringify(buildWorkerState(busy, chat, atMs)))
    renameSync(tmp, path)
  } catch {}
}
