// pub-parity · 启动自检 spawn_failed（行为清单 14）：隔离拉起后在 init 之前非 0 退出，且不是 dispatcher 主动停。
// 单独成文件（内存位不清零）。接缝假设见 wm_harness.ts；"主动停"用 manager 的 stop/kill/shutdown 之一。
import { test, expect } from 'bun:test'
import { boot, sleep } from './wm_harness'

const h = await boot()
h.reset()

test('dispatcher主动停_即使退出码非0也不置位_下一次拉起仍隔离', async () => {
  const m = h.newManager()
  await h.launch(m)
  const stop = m.stop ?? m.kill ?? m.shutdown
  if (typeof stop !== 'function') throw new Error('找不到主动停止的方法（候选 stop / kill / shutdown）')
  const from = h.logs.length
  await stop.call(m)
  await sleep(120)
  expect(h.isoLines(from).filter(l => l.includes('init_check=fail'))).toEqual([])
  const next = await h.launch(m)
  expect(h.argAfter(next.args, '--setting-sources')).toBe('project,local')
  await sleep(1300)
})

test('隔离拉起后init之前以非0退出_记spawn_failed_下一次拉起参数环境与改动前相同', async () => {
  h.reset()
  const legacy = await h.legacy()
  const m = h.newManager()
  const first = await h.launch(m)
  const from = h.logs.length
  first.child.exit(1)
  await sleep(120)
  expect(h.isoLines(from)).toContain('WORKER-ISOLATION init_check=fail reason=spawn_failed')
  const from2 = h.logs.length
  const second = await h.launch(m)
  expect(h.normArgs(second.args)).toEqual(h.normArgs(legacy.args))
  expect(second.env).toEqual(legacy.env)
  expect(h.isoLines(from2)[0]).toMatch(/^WORKER-ISOLATION mode=on status=degraded reason=spawn_failed notes=[a-z_,-]+$/)
})
