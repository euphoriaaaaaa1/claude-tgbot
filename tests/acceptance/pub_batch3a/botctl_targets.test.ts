// pub-batch3a · /stopbot /startbot 目标名单与解析（行为清单 18-23）。
// 只经公开函数 botCtlTargets / parseBotCtl / parseBotCtlWithBots 观察。
// 名单来源：环境变量 DISPATCHER_CTL_BOTS（优先）否则配置目录里的 <名>.yml。
// 配置目录用 HUB_CONFIGS_DIR 指到临时目录（同时设 CLAUDEBOT_CONFIG_DIR 兜底，两个都指向同一处）。
import { afterEach, beforeEach, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as botctl from '../../../dispatcher/botctl'

// 命名空间导入：接口里的函数还不存在时，让用例以断言失败（而非导入错误）变红
const need = (name: string): any => {
  const f = (botctl as any)[name]
  expect(typeof f).toBe('function')
  return f
}
const parseBotCtl = (t: string): any => need('parseBotCtl')(t)
const parseBotCtlWithBots = (t: string, bots: string[]): any => need('parseBotCtlWithBots')(t, bots)
const botCtlTargets = (): string[] => need('botCtlTargets')()

const ENV_KEYS = ['HUB_CONFIGS_DIR', 'CLAUDEBOT_CONFIG_DIR', 'DISPATCHER_CTL_BOTS']
const saved: Record<string, string | undefined> = {}
let dir = ''

beforeEach(() => {
  for (const k of ENV_KEYS) { saved[k] = process.env[k]; delete process.env[k] }
  dir = mkdtempSync(join(tmpdir(), 'pb3a-botctl-'))
  pointConfigsAt(dir)
})
afterEach(() => {
  for (const k of ENV_KEYS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k] }
  if (dir.startsWith(tmpdir())) rmSync(dir, { recursive: true, force: true })
})

function pointConfigsAt(d: string) { process.env.HUB_CONFIGS_DIR = d; process.env.CLAUDEBOT_CONFIG_DIR = d }
const touch = (...names: string[]) => { for (const n of names) writeFileSync(join(dir, n), '# fake\n') }
const usageOf = (r: unknown): string => (r && typeof r === 'object' && 'usage' in (r as any) ? String((r as any).usage) : '')

test('18_配置目录里只有合法bot名的yml才进名单_stopbot_alpha成功_usage的可选名单正好是alpha和beta', () => {
  touch('alpha.yml', 'beta.yml', '_global.yml', 'a b.yml', 'x.yaml')
  expect(parseBotCtl('/stopbot alpha')).toEqual({ cmd: 'stopbot', target: 'alpha' })
  const usage = usageOf(parseBotCtl('/stopbot nobody'))
  expect(usage).toContain('用法')
  expect(usage).toContain('alpha / beta')
  expect(usage).not.toMatch(/beta \//)           // 名单到 beta 为止，没有第三项
  expect(usage).not.toContain('_global')
  expect(usage).not.toContain('a b')
})

test('19_运行时新加gamma_yml_不重启startbot_gamma就成功', () => {
  touch('alpha.yml')
  expect(usageOf(parseBotCtl('/startbot gamma'))).toContain('用法')   // 加之前不在名单
  touch('gamma.yml')
  expect(parseBotCtl('/startbot gamma')).toEqual({ cmd: 'startbot', target: 'gamma' })
})

test('20a_DISPATCHER_CTL_BOTS设了就以它为准_逗号后空格被去掉_大小写不敏感_目录里的alpha不再可用', () => {
  touch('alpha.yml')
  process.env.DISPATCHER_CTL_BOTS = 'x, Y'
  const r: any = parseBotCtl('/stopbot y')
  expect(r?.cmd).toBe('stopbot')
  expect(String(r?.target).toLowerCase()).toBe('y')   // 接口文件写 target y；名单条目为 'Y'，大小写口径见报告
  expect(usageOf(parseBotCtl('/stopbot alpha'))).toContain('用法')
  expect(parseBotCtl('/stopbot alpha')).not.toHaveProperty('target')
})

for (const bad of [' , ', '../z']) {
  test(`20b_DISPATCHER_CTL_BOTS取${JSON.stringify(bad)}_无效_回落到按目录派生的名单`, () => {
    touch('alpha.yml')
    process.env.DISPATCHER_CTL_BOTS = bad
    expect(botCtlTargets()).toEqual(['alpha'])
    expect(parseBotCtl('/stopbot alpha')).toEqual({ cmd: 'stopbot', target: 'alpha' })
  })
}

test('21_目录不存在且环境变量未设_usage提示尚未配置', () => {
  pointConfigsAt(join(dir, 'no-such-subdir'))
  const r = parseBotCtl('/stopbot alpha')
  expect(r).not.toBeNull()
  expect(usageOf(r)).toContain('尚未配置')
})

test('22_只看首行_stopbot_alpha后面带第二行仍得到命令', () => {
  touch('alpha.yml', 'beta.yml')
  expect(parseBotCtlWithBots('/stopbot alpha\n第二行', ['alpha', 'beta'])).toEqual({ cmd: 'stopbot', target: 'alpha' })
  expect(parseBotCtl('/stopbot alpha\n第二行')).toEqual({ cmd: 'stopbot', target: 'alpha' })
})

test('23_目录里是Alpha_yml_小写和大写输入都得到名单里的原样条目Alpha', () => {
  touch('Alpha.yml')
  expect(parseBotCtl('/stopbot alpha')).toEqual({ cmd: 'stopbot', target: 'Alpha' })
  expect(parseBotCtl('/stopbot ALPHA')).toEqual({ cmd: 'stopbot', target: 'Alpha' })
})
