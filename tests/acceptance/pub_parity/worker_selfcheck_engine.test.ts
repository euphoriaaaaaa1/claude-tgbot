// pub-parity · 启动自检 engine_not_loaded（行为清单 14）：生图技能已挂入而 init 技能清单里没有 novelai-skill。
// 单独成文件（内存位不清零）。接缝假设见 wm_harness.ts。
import { test, expect } from 'bun:test'
import { boot, sleep } from './wm_harness'

const h = await boot()
h.reset()
const initEvent = (skills: string[]) => ({ type: 'system', subtype: 'init', session_id: 's1', skills, tools: [], model: 'm' })

test('init里有novelai-skill_不置位_下一次拉起仍隔离', async () => {
  const m = h.newManager()
  const first = await h.launch(m)
  const from = h.logs.length
  first.child.emitLine(initEvent(['update-config', 'novelai-skill']))
  await sleep(120)
  expect(h.isoLines(from).filter(l => l.includes('init_check=fail'))).toEqual([])
  const second = await h.launch(m)
  expect(h.argAfter(second.args, '--setting-sources')).toBe('project,local')
})

test('生图已挂入而init里没有novelai-skill_记engine_not_loaded_下一次拉起参数环境与改动前相同', async () => {
  h.reset()
  const legacy = await h.legacy()
  const m = h.newManager()
  const first = await h.launch(m)
  expect(first.args.includes(h.shared)).toBe(true)     // 前提：这一次确实挂了共享目录
  const from = h.logs.length
  first.child.emitLine(initEvent(['update-config']))
  await sleep(120)
  expect(h.isoLines(from)).toContain('WORKER-ISOLATION init_check=fail reason=engine_not_loaded')
  const from2 = h.logs.length
  const second = await h.launch(m)
  expect(h.normArgs(second.args)).toEqual(h.normArgs(legacy.args))
  expect(second.env).toEqual(legacy.env)
  expect(h.isoLines(from2)[0]).toMatch(/^WORKER-ISOLATION mode=on status=degraded reason=engine_not_loaded notes=[a-z_,-]+$/)
})
