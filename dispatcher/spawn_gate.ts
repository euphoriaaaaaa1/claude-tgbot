/**
 * spawn 调度闸门（D-03 §二 6-7 / D-04 §三 8-11）。
 *
 * 背景（审计）：dispatcher 原先用 spawnSync 同步拉起 worker —— 等锁/慢启动期间
 * 整个事件循环被卡死（HTTP 端口、消息处理全停）；且 spawn 失败也照报 spawned:true。
 * 本模块把 spawn 收成异步单飞闸门：
 *   - 异步：runSpawn 与超时都走 Promise，绝不阻塞事件循环（契约 8）；
 *   - 时间上限：超时按失败语义收尾——killSession + 告警 + spawned:false（契约 9）；
 *   - 单飞：同一 chat 进行中的那次复用，不并行拉起第二个（契约 10）；
 *   - 失败可重试：单飞只在"进行中"生效，settle 后立即允许下一次触发；
 *   - 成败可判：code!==0 / 未正常结束（null）→ spawned:false + 告警（契约 6）。
 *
 * "消息不丢"不在本模块的职责内：调用方必须在 ensure() 之外无条件写 inbox，
 * 不得把写 inbox 挪进 spawn 成功分支。
 */
export interface SpawnGateDeps {
  /** 替身注入点（真实实现=拉起 worker，resolve 子进程退出码；未正常结束用 null）。 */
  runSpawn: (chatId: string) => Promise<{ code: number | null }>
  /** 失败清理（公开版 worker-manager 的会话终止注入点）；可选——缺省时只告警。 */
  killSession?: () => Promise<void>
  /** 告警出口（stderr / 测试收集数组）。成功不打日志。 */
  log: (line: string) => void
  /** 时间上限（毫秒）。缺省 180s：覆盖 worker 启动内部的等锁 + 正常就绪时间。 */
  timeoutMs?: number
}

export interface SpawnGate {
  ensure(chatId: string): Promise<{ spawned: boolean }>
}

const DEFAULT_TIMEOUT_MS = 180_000

export function createSpawnGate(deps: SpawnGateDeps): SpawnGate {
  const timeoutMs = deps.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const inFlight = new Map<string, Promise<{ spawned: boolean }>>()

  async function attempt(chatId: string): Promise<{ spawned: boolean }> {
    let code: number | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<'timeout'>(resolve => {
      timer = setTimeout(() => resolve('timeout'), timeoutMs)
    })
    let raced: 'done' | 'timeout'
    try {
      raced = await Promise.race([
        deps.runSpawn(chatId).then(r => { code = r?.code ?? null; return 'done' as const }),
        timeout,
      ])
    } catch {
      // runSpawn 自身抛（子进程层崩）→ 与"未正常结束"同等处理
      raced = 'done'
      code = null
    } finally {
      if (timer !== undefined) clearTimeout(timer)
    }
    if (raced === 'timeout') {
      deps.log(`spawn gate: spawn 超时 ${timeoutMs}ms 未完成 (chat=${chatId}) → 按失败清理`)
      if (deps.killSession) {
        try { await deps.killSession() } catch { /* 清理失败不改变"失败"结论，仍需告警已落 */ }
      }
      return { spawned: false }
    }
    if (code === 0) return { spawned: true }   // 成功不打日志，避免误报
    deps.log(`spawn gate: worker 启动未成功 (chat=${chatId} code=${code ?? 'null'})`)
    return { spawned: false }
  }

  return {
    ensure(chatId: string): Promise<{ spawned: boolean }> {
      const running = inFlight.get(chatId)
      if (running) return running   // 进行中：复用同一次 spawn，不并行拉起第二个
      const p = attempt(chatId).finally(() => {
        if (inFlight.get(chatId) === p) inFlight.delete(chatId)   // settle 即清：失败后允许重试
      })
      inFlight.set(chatId, p)
      return p
    },
  }
}
