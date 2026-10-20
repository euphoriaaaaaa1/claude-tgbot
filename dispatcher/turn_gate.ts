/**
 * 跨聊天互斥的放行判定 · 纯逻辑层（A2-04）。
 *
 * 背景：worker 一轮回复里排在后面的 reply/react 默认回到"最近一条消息所在的聊天"。
 * 旧实现按定时器近似放行（工具调用入口起静默 5s / 投递起硬上限 30s），而一次 reply
 * 实测中位 15~20 秒——窗口一开后另一个聊天的消息就被投了进来，reply 的默认目标被改，
 * 于是"私聊的话发进群"。
 *
 * 这里把放行条件改成"回合真正结束的精确信号"（CLI Stop 钩子 touch 的标记文件 mtime 变化），
 * 而不是任何计时近似：回合没结束（哪怕两次工具调用之间长静默）就绝不放行；信号不可用时
 * 逐字退回旧行为，不引入新风险。判定全放这里（可注入时钟、不碰文件/网络/MCP），
 * worker-plugin.ts 只做接线。
 */

export type GateState = {
  inFlightChatId: string
  inflightTools: number
  deliveredAtMs: number
  lastToolEndMs: number | null
  turnEndedAtMs: number | null
}

export type GateDecision = { release: boolean; recheckInMs: number | null; forced: boolean }

export type TurnGateOpts = {
  quietMs?: number            // 回合结束后再等这么久才放行（等最后一批工具结果落地）
  maxTurnMs?: number          // 绝对上限：自投递起这么久还没见回合结束 → 强制放行（仅防死锁）
  fallbackHardCapMs?: number  // 降级模式：自投递起的硬上限
  hasTurnMarker: boolean      // 回合结束信号是否可用；false = 降级，逐字等价现状
}

export type TurnGate = {
  onDeliver(chatId: string, atMs: number): void
  onToolStart(atMs: number): void
  onToolEnd(atMs: number): void
  onTurnEnd(atMs: number): void
  decide(atMs: number): GateDecision
  onRelease(): void
  reset(): void
}

const DEFAULT_QUIET_MS = 1500
const DEFAULT_MAX_TURN_MS = 600_000           // 10 分钟
const DEFAULT_FALLBACK_HARD_CAP_MS = 30_000
// 降级模式工具调用入口的重置窗口。固定 5000：现状 worker-plugin 的 QUIET_MS，
// 降级要"逐字等价现状"，故不跟随 quietMs（新逻辑的 1500）。
const DEGRADED_TOOL_QUIET_MS = 5000

/**
 * 轮询基线比较（契约 9）：给定启动时记下的基线 mtime 与当前 mtime，判断是否算一次"变化"。
 * 纯函数，便于单测；worker 用它决定要不要 onTurnEnd。
 * - 读不到当前 mtime（文件不存在 / 出错）→ false：绝不把"读失败"当回合结束。
 * - 启动时没有该文件、现在出现了 → true：这正是第一轮的 Stop 钩子建出来的。
 * - 否则：mtime 与基线不同即算一次变化（不比较大小，防止文件被原子替换后 mtime 回退漏判）。
 */
export function turnMarkerChanged(baselineMs: number | null, currentMs: number | null): boolean {
  if (currentMs === null) return false
  if (baselineMs === null) return true
  return currentMs !== baselineMs
}

export function createTurnGate(opts: TurnGateOpts): TurnGate {
  const quietMs = opts.quietMs ?? DEFAULT_QUIET_MS
  const maxTurnMs = opts.maxTurnMs ?? DEFAULT_MAX_TURN_MS
  const fallbackHardCapMs = opts.fallbackHardCapMs ?? DEFAULT_FALLBACK_HARD_CAP_MS
  const hasTurnMarker = opts.hasTurnMarker === true

  let inFlightChatId = ''
  let inflightTools = 0
  let deliveredAtMs = 0
  let lastToolEndMs: number | null = null
  let turnEndedAtMs: number | null = null
  // 降级模式专用：下一次到期放行的绝对时刻（投递/工具入口各自重置）
  let degradedDeadlineAtMs: number | null = null

  function clear(): void {
    inFlightChatId = ''
    inflightTools = 0
    deliveredAtMs = 0
    lastToolEndMs = null
    turnEndedAtMs = null
    degradedDeadlineAtMs = null
  }

  return {
    onDeliver(chatId, atMs) {
      inFlightChatId = chatId
      deliveredAtMs = atMs
      // 新的一轮：清掉上一轮的回合结束标记（同聊天连发时，新消息会开启新回合，等它自己的 Stop）
      turnEndedAtMs = null
      lastToolEndMs = null
      // 在飞工具计数不在这里清零：同聊天连发可能在工具在飞时投递，清零会让计数泄漏成负数
      if (!hasTurnMarker) degradedDeadlineAtMs = atMs + fallbackHardCapMs
    },
    onToolStart(atMs) {
      inflightTools++
      if (!hasTurnMarker) degradedDeadlineAtMs = atMs + DEGRADED_TOOL_QUIET_MS
    },
    onToolEnd(atMs) {
      if (inflightTools > 0) inflightTools--   // 夹到 0：强制放行后残留的在飞工具结束时不会变负
      lastToolEndMs = atMs
    },
    onTurnEnd(atMs) {
      if (!hasTurnMarker) return               // 降级模式：回合标记无效果（契约 6）
      turnEndedAtMs = atMs
    },
    decide(atMs) {
      // 没有在飞轮次 → 初态（含 reset/onRelease 之后）
      if (inFlightChatId === '') return { release: false, recheckInMs: null, forced: false }

      if (!hasTurnMarker) {
        // 降级：投递起 fallbackHardCapMs，工具入口重置为 5000ms，到点即放行——逐字等价现状
        if (degradedDeadlineAtMs === null) return { release: false, recheckInMs: null, forced: false }
        if (atMs >= degradedDeadlineAtMs) return { release: true, recheckInMs: null, forced: false }
        return { release: false, recheckInMs: degradedDeadlineAtMs - atMs, forced: false }
      }

      // 绝对上限（仅防死锁）：未见回合结束且超时 → 强制放行，调用方据此写 turn_force_release
      if (atMs - deliveredAtMs > maxTurnMs) return { release: true, recheckInMs: null, forced: true }

      // 回合结束信号到了且没有工具在飞：等满 quietMs 才放行（基线取回合结束与最后一次工具结束的较晚者）
      if (turnEndedAtMs !== null && inflightTools === 0) {
        const baseline = Math.max(turnEndedAtMs, lastToolEndMs ?? turnEndedAtMs)
        const waited = atMs - baseline
        if (waited >= quietMs) return { release: true, recheckInMs: null, forced: false }
        return { release: false, recheckInMs: quietMs - waited, forced: false }
      }

      // 回合没结束，或仍有工具在飞 → 绝不放行（不再有任何基于静默的提前放行）
      return { release: false, recheckInMs: quietMs, forced: false }
    },
    onRelease() { clear() },
    reset() { clear() },
  }
}
