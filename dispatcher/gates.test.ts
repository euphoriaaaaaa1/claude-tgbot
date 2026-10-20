// pause_gate / spawn_gate / turn_gate / worker_state 单测。全部使用假时钟与临时目录。
import { afterEach, describe, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { makePauseCheck } from './pause_gate'
import { createSpawnGate } from './spawn_gate'
import { createTurnGate, turnMarkerChanged } from './turn_gate'
import { buildWorkerState, busyFromQueue, writeWorkerStateFile } from './worker_state'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't19-gates-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

describe('pause_gate', () => {
  test('无标记 → 未暂停', () => {
    const d = mkDir()
    expect(makePauseCheck('bot1', { HOME: d })()).toBeNull()
  })

  test('全局标记优先', () => {
    const d = mkDir()
    writeFileSync(join(d, '.claudebotlife.pause'), '')
    expect(makePauseCheck('bot1', { HOME: d })()).toBe('global')
  })

  test('单 bot 标记；空 bot 名不判单 bot', () => {
    const d = mkDir()
    writeFileSync(join(d, '.claudebotlife.pause-bot1'), '')
    expect(makePauseCheck('bot1', { HOME: d })()).toBe('bot:bot1')
    expect(makePauseCheck('bot2', { HOME: d })()).toBeNull()
    expect(makePauseCheck('', { HOME: d })()).toBeNull()
  })

  test('显式覆盖路径', () => {
    const d = mkDir()
    writeFileSync(join(d, 'g'), '')
    expect(makePauseCheck('b', { PAUSE_FILE_GLOBAL: join(d, 'g') })()).toBe('global')
    writeFileSync(join(d, '.claudebotlife.pause-b'), '')
    expect(makePauseCheck('b', { PAUSE_FILE_GLOBAL: join(d, 'none'), PAUSE_FILE_BOT_DIR: d })()).toBe('bot:b')
  })
})

describe('spawn_gate', () => {
  test('成功与失败语义', async () => {
    const logs: string[] = []
    const ok = createSpawnGate({ runSpawn: async () => ({ code: 0 }), log: l => logs.push(l) })
    expect(await ok.ensure('c1')).toEqual({ spawned: true })
    expect(logs).toEqual([])

    const bad = createSpawnGate({ runSpawn: async () => ({ code: 2 }), log: l => logs.push(l) })
    expect(await bad.ensure('c1')).toEqual({ spawned: false })
    expect(logs.at(-1)).toContain('code=2')

    const thrown = createSpawnGate({ runSpawn: async () => { throw new Error('x') }, log: l => logs.push(l) })
    expect(await thrown.ensure('c1')).toEqual({ spawned: false })
  })

  test('同一 chat 单飞，settle 后允许重试；不同 chat 并行', async () => {
    let calls = 0
    let release!: () => void
    const gate = createSpawnGate({
      runSpawn: async () => { calls++; await new Promise<void>(r => { release = r }); return { code: 0 } },
      log: () => {},
    })
    const p1 = gate.ensure('c1')
    const p2 = gate.ensure('c1')
    expect(calls).toBe(1)
    expect(p1).toBe(p2)
    release()
    await p1
    const p3 = gate.ensure('c1')
    expect(calls).toBe(2)
    release()
    await p3
  })

  test('超时按失败收尾并调用清理', async () => {
    let killed = 0
    const logs: string[] = []
    const gate = createSpawnGate({
      runSpawn: () => new Promise(() => {}),
      killSession: async () => { killed++ },
      log: l => logs.push(l),
      timeoutMs: 20,
    })
    expect(await gate.ensure('c1')).toEqual({ spawned: false })
    expect(killed).toBe(1)
    expect(logs.some(l => l.includes('超时'))).toBe(true)
  })
})

describe('turn_gate（有回合标记）', () => {
  const opts = { hasTurnMarker: true, quietMs: 100, maxTurnMs: 1000 }

  test('回合未结束绝不放行；结束后等满 quiet', () => {
    const g = createTurnGate(opts)
    g.onDeliver('c1', 0)
    expect(g.decide(500).release).toBe(false)
    g.onTurnEnd(500)
    expect(g.decide(550)).toEqual({ release: false, recheckInMs: 50, forced: false })
    expect(g.decide(600).release).toBe(true)
  })

  test('工具在飞时不放行；baseline 取回合结束与最后工具结束的较晚者', () => {
    const g = createTurnGate(opts)
    g.onDeliver('c1', 0)
    g.onToolStart(10)
    g.onTurnEnd(20)
    expect(g.decide(100).release).toBe(false)
    g.onToolEnd(150)
    expect(g.decide(200).recheckInMs).toBe(50)
    expect(g.decide(250).release).toBe(true)
  })

  test('超过绝对上限强制放行', () => {
    const g = createTurnGate(opts)
    g.onDeliver('c1', 0)
    expect(g.decide(1001)).toEqual({ release: true, recheckInMs: null, forced: true })
  })

  test('新投递清掉上一轮回合标记；onRelease/reset 回初态', () => {
    const g = createTurnGate(opts)
    g.onDeliver('c1', 0)
    g.onTurnEnd(10)
    g.onDeliver('c1', 20)
    expect(g.decide(200).release).toBe(false)
    g.onRelease()
    expect(g.decide(2000)).toEqual({ release: false, recheckInMs: null, forced: false })
    g.onDeliver('c1', 0)
    g.reset()
    expect(g.decide(2000).release).toBe(false)
  })

  test('turnMarkerChanged 边界', () => {
    expect(turnMarkerChanged(null, null)).toBe(false)
    expect(turnMarkerChanged(null, 1)).toBe(true)
    expect(turnMarkerChanged(1, null)).toBe(false)
    expect(turnMarkerChanged(1, 2)).toBe(true)
    expect(turnMarkerChanged(1, 1)).toBe(false)
  })
})

describe('turn_gate（降级模式）', () => {
  const opts = { hasTurnMarker: false, fallbackHardCapMs: 30_000 }

  test('投递后 30s 放行；工具入口重置为 5s；回合标记无效', () => {
    const g = createTurnGate(opts)
    g.onDeliver('c1', 0)
    expect(g.decide(29_999)).toEqual({ release: false, recheckInMs: 1, forced: false })
    expect(g.decide(30_000).release).toBe(true)
    g.onRelease()
    g.onDeliver('c1', 0)
    g.onToolStart(20_000)
    expect(g.decide(24_999).release).toBe(false)
    expect(g.decide(25_000).release).toBe(true)
    g.onRelease()
    g.onDeliver('c1', 0)
    g.onTurnEnd(1)
    expect(g.decide(100).release).toBe(false)
  })
})

describe('worker_state', () => {
  test('buildWorkerState 与 busyFromQueue', () => {
    expect(buildWorkerState(true, 'c1', 5)).toEqual({ v: 1, busy: true, at: 5, chat: 'c1' })
    expect(busyFromQueue(0)).toBe(false)
    expect(busyFromQueue(1)).toBe(true)
  })

  test('原子写状态文件；写失败静默', () => {
    const d = mkDir()
    const p = join(d, '.worker-state.json')
    writeWorkerStateFile(p, true, 'c1', 123)
    expect(JSON.parse(readFileSync(p, 'utf8'))).toEqual({ v: 1, busy: true, at: 123, chat: 'c1' })
    expect(existsSync(p)).toBe(true)
    expect(() => writeWorkerStateFile(join(d, 'no', 'dir', 'x.json'), false, '', 1)).not.toThrow()
  })
})
