// pub-parity · 启动自检 flag_ignored（行为清单 14）：init 里 flag 已关掉的技能仍在 -> 记 init_check=fail，下一次拉起退回旧参数。
// 单独成文件：自检内存位在同一进程内不会清零，每个"置位"场景独占一个进程。接缝假设见 wm_harness.ts。
import { test, expect } from 'bun:test'
import { statSync } from 'node:fs'
import { boot, sleep } from './wm_harness'

const h = await boot()
h.reset()
const initEvent = (skills: string[]) => ({ type: 'system', subtype: 'init', session_id: 's1', skills, tools: [], model: 'm' })

test('退出码0且init之前退出_不置位_下一次拉起仍隔离_没有init_check日志', async () => {
  const m = h.newManager()
  const first = await h.launch(m)
  const from = h.logs.length
  first.child.exit(0)
  await sleep(60)
  const second = await h.launch(m)
  expect(h.argAfter(second.args, '--setting-sources')).toBe('project,local')
  expect(h.isoLines(from).filter(l => l.includes('init_check=fail'))).toEqual([])
  await sleep(1300)                                   // 让既有的退避重启（1s）先跑完，别污染后面的用例
})

test('init里含已关掉的内置技能doctor_记flag_ignored_下一次拉起参数环境与改动前相同_不再写flag', async () => {
  h.reset()
  const legacy = await h.legacy()
  const m = h.newManager()
  const first = await h.launch(m)
  const from = h.logs.length
  first.child.emitLine(initEvent(['doctor', 'novelai-skill']))
  await sleep(120)
  expect(h.isoLines(from)).toContain('WORKER-ISOLATION init_check=fail reason=flag_ignored')
  const mtime = statSync(h.flag).mtimeMs
  const from2 = h.logs.length
  const second = await h.launch(m)
  expect(h.normArgs(second.args)).toEqual(h.normArgs(legacy.args))
  expect(second.env).toEqual(legacy.env)
  const lines = h.isoLines(from2)
  expect(lines.length).toBe(1)
  expect(lines[0]).toMatch(/^WORKER-ISOLATION mode=on status=degraded reason=flag_ignored notes=[a-z_,-]+$/)
  expect(statSync(h.flag).mtimeMs).toBe(mtime)        // 自检置位时不调准备函数、什么都不写
})
