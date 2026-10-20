// 白盒单测：波次收集 + 合并落盘（burst_inbox.ts）。假时钟 + 临时 inbox 目录，
// 覆盖验收层测不到的那三块：真实计时、access.json 兜底、合并落盘/降级的文件后果。
// 夹具全假数据（FAKECHAT-*），断言不回显正文长文。
import { test, expect, afterAll } from 'bun:test'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'
import { createBurstCollector, readBurstCfg, DEFAULT_BURST } from './burst_inbox'

const ROOT = mkdtempSync(join(tmpdir(), 'burst-test-'))
afterAll(() => { if (ROOT.startsWith(tmpdir())) rmSync(ROOT, { recursive: true, force: true }) })

let caseSeq = 0
type Rig = {
  dir: string; dest: string; out: string[]; logs: string[]
  at: (ms: number) => void; add: (o: Record<string, unknown>) => string
  c: ReturnType<typeof createBurstCollector>
}
/** 一套隔离的假环境：假时钟 at(ms) 绝对设定，add() 写一个 inbox 原件并返回路径 */
function rig(cfg = { windowMs: 5000, maxMs: 12000 }): Rig {
  const dir = join(ROOT, `case${++caseSeq}`)
  const dest = join(dir, '.pending')
  mkdirSync(dir, { recursive: true })
  let clock = 1_000_000
  let seq = 0
  const out: string[] = []
  const logs: string[] = []
  const c = createBurstCollector({
    ...cfg, destDir: dest, now: () => clock,
    deliver: p => out.push(p), log: l => logs.push(l),
  })
  const add = (o: Record<string, unknown>): string => {
    const p = join(dir, `m${++seq}.json`)
    writeFileSync(p, JSON.stringify({
      chat_id: 'FAKECHAT-1', message_id: seq, from_id: 'U1', scene: 'private',
      chat_type: 'private', ts: `2026-08-25T0${seq}:00:00.000Z`, text: `T${seq}`, ...o,
    }))
    return p
  }
  return { dir, dest, out, logs, at: (ms: number) => { clock = ms }, add, c }
}
const readOut = (p: string) => JSON.parse(readFileSync(p, 'utf8'))

test('三条连发攒成一波：满窗才投，且只投一个合并件', () => {
  const r = rig()
  r.at(1000); r.c.arrive(r.add({ text: 'A1' }))
  r.at(1300); r.c.arrive(r.add({ text: 'B2' }))
  r.at(1600); r.c.arrive(r.add({ text: 'C3' }))
  r.at(6000); r.c.tick()            // 距末条 4400ms，还不到
  expect(r.out).toEqual([])
  r.at(6600); r.c.tick()            // 距末条恰好 5000ms
  expect(r.out.length).toBe(1)
  const m = readOut(r.out[0]!)
  expect([m.text, m.message_id, m.chat_id]).toEqual(['A1\nB2\nC3', 1, 'FAKECHAT-1'])
  expect(m.ts).toBe('2026-08-25T03:00:00.000Z')          // ts 取末条
  expect(readdirSync(r.dir).filter(f => f.endsWith('.json'))).toEqual([])  // 原件已清
  expect(r.c.waves()).toBe(0)
})

test('两条间隔超窗：各自成波，投两条、都不合并', () => {
  const r = rig()
  r.at(1000); r.c.arrive(r.add({}))
  r.at(6000); r.c.tick()
  r.at(6100); r.c.arrive(r.add({}))
  r.at(11200); r.c.tick()
  expect(r.out.length).toBe(2)
  expect(existsSync(r.dest)).toBe(false)   // 单条波不产生合并件，投的就是原件
  expect(r.out.every(p => p.startsWith(r.dir) && !p.includes('.pending'))).toBe(true)
})

test('连发不停：自首条起满 12s 强投，之后的另起一波', () => {
  const r = rig()
  for (let i = 0; i < 12; i++) { r.at(1000 + i * 1000); r.c.arrive(r.add({})); r.c.tick() }
  expect(r.out).toEqual([])                // 每秒一条，窗口永远续上，靠封顶才投
  r.at(13000); r.c.tick()                  // 距首条 12000ms
  expect(r.out.length).toBe(1)
  expect(readOut(r.out[0]!).text.split('\n').length).toBe(12)
  r.at(13100); r.c.arrive(r.add({ text: 'NEXT' }))
  expect(r.c.waves()).toBe(1)
  r.at(18200); r.c.tick()
  expect(r.out.length).toBe(2)
  expect(readOut(r.out[1]!).text).toBe('NEXT')   // 单条波=原件直投
})

test('波内混入图片：文本波先投、图片单投，顺序不乱且图片不被合并', () => {
  const r = rig()
  r.at(1000); r.c.arrive(r.add({ text: 'A1' }))
  r.at(1200); r.c.arrive(r.add({ text: 'B2' }))
  r.at(1400); const img = r.add({ text: '', image_path: '/FAKE/img.png' })
  r.c.arrive(img)
  expect(r.out.length).toBe(2)
  expect(r.out[1]).toBe(img)                        // 图片原件单投
  expect(readOut(r.out[0]!).text).toBe('A1\nB2')    // 合并件不含图片
  expect(existsSync(img)).toBe(true)
  expect(r.c.waves()).toBe(0)
})

test('内部合成消息与 peer bot 消息不并进波，照旧截波单投', () => {
  for (const extra of [{ text: '[director] 该你说话了' }, { is_bot_sender: true, text: 'peer' }]) {
    const r = rig()
    r.at(1000); const a = r.add({ text: 'A1' })
    r.c.arrive(a)
    r.at(1200); const special = r.add(extra)
    r.c.arrive(special)
    expect(r.out).toEqual([a, special])   // 波（单条）先投，合成消息随后单投，正文互不沾
    expect(r.c.waves()).toBe(0)
  }
})

test('群里换人说话不合并：后一个人截波单投，不被按在前一个人头上', () => {
  const r = rig()
  r.at(1000); r.c.arrive(r.add({ chat_id: '-FAKEGRP', from_id: 'U1', text: 'A1' }))
  r.at(1200); r.c.arrive(r.add({ chat_id: '-FAKEGRP', from_id: 'U1', text: 'B2' }))
  r.at(1400); const other = r.add({ chat_id: '-FAKEGRP', from_id: 'U2', text: 'C3' })
  r.c.arrive(other)
  expect(r.out.length).toBe(2)
  expect(r.out[1]).toBe(other)
  expect(readOut(r.out[0]!).from_id).toBe('U1')
})

test('多 chat 交错：各自独立成波，正文不串', () => {
  const r = rig()
  r.at(1000); r.c.arrive(r.add({ chat_id: 'FAKECHAT-1', text: 'A1' }))
  r.at(1100); r.c.arrive(r.add({ chat_id: '-FAKEGRP', from_id: 'U9', text: 'G1' }))
  r.at(1200); r.c.arrive(r.add({ chat_id: 'FAKECHAT-1', text: 'A2' }))
  r.at(1300); r.c.arrive(r.add({ chat_id: '-FAKEGRP', from_id: 'U9', text: 'G2' }))
  expect(r.c.waves()).toBe(2)
  r.at(6400); r.c.tick()
  expect(r.out.length).toBe(2)
  const texts = r.out.map(p => readOut(p).text)
  expect(texts.sort()).toEqual(['A1\nA2', 'G1\nG2'])
})

test('合并失败（原件半路损坏）→ 按原顺序逐条降级投，原件一个不删', () => {
  const r = rig()
  r.at(1000); const a = r.add({ text: 'A1' })
  r.c.arrive(a)
  r.at(1200); const b = r.add({ text: 'B2' })
  r.c.arrive(b)
  writeFileSync(b, '{not json')            // 进波之后原件被写坏
  r.at(6300); r.c.tick()
  expect(r.out).toEqual([a, b])            // 顺序保持，绝不丢
  expect([existsSync(a), existsSync(b)]).toEqual([true, true])
  expect(r.logs.some(l => l.startsWith('burst_degrade chat=FAKECHAT-1 count=2'))).toBe(true)
})

test('原件在投递前消失：跳过，不炸也不投空路径', () => {
  const r = rig()
  r.at(1000); const a = r.add({ text: 'A1' })
  r.c.arrive(a)
  rmSync(a, { force: true })
  r.at(6100); r.c.tick()
  expect(r.out).toEqual([])
  expect(r.c.waves()).toBe(0)
})

test('认不出的原件（坏 JSON / 无 chat_id）交回调用方，不进波次层', () => {
  const r = rig()
  const bad = join(r.dir, 'bad.json')
  writeFileSync(bad, '{not json')
  expect(r.c.arrive(bad)).toBe(false)
  expect(r.c.arrive(r.add({ chat_id: '' }))).toBe(false)
  expect(r.c.arrive(join(r.dir, 'missing.json'))).toBe(false)
  expect([r.out.length, r.c.waves()]).toEqual([0, 0])
})

test('windowMs=0 关闭：collector 不接管任何消息（调用方走原路径）', () => {
  const r = rig({ windowMs: 0, maxMs: 12000 })
  expect(r.c.enabled).toBe(false)
  expect(r.c.arrive(r.add({}))).toBe(false)
  r.at(9_999_999); r.c.tick()
  expect([r.out.length, r.c.waves()]).toEqual([0, 0])
})

test('同一文件重复到达（fs.watch 连报两次）只算一条，正文不重复', () => {
  const r = rig()
  r.at(1000); const a = r.add({ text: 'A1' })
  r.c.arrive(a); r.c.arrive(a)
  r.at(1200); r.c.arrive(r.add({ text: 'B2' }))
  r.at(6300); r.c.tick()
  expect(readOut(r.out[0]!).text).toBe('A1\nB2')
})

test('空波 tick 幂等，不产生任何投递', () => {
  const r = rig()
  r.at(5000); r.c.tick(); r.c.tick()
  expect([r.out.length, r.logs.length]).toEqual([0, 0])
})

test('日志只有条数与 chat_id，绝不带正文', () => {
  const r = rig()
  r.at(1000); r.c.arrive(r.add({ text: 'SECRET-BODY-1' }))
  r.at(1200); r.c.arrive(r.add({ text: 'SECRET-BODY-2' }))
  r.at(6300); r.c.tick()
  expect(r.logs).toEqual(['burst_merged chat=FAKECHAT-1 count=2'])
})

test('readBurstCfg：缺文件/缺字段 → 5000/12000', () => {
  const d = join(ROOT, 'cfg-missing')
  mkdirSync(d, { recursive: true })
  expect(readBurstCfg(d)).toEqual(DEFAULT_BURST)
  writeFileSync(join(d, 'access.json'), JSON.stringify({ allowFrom: ['FAKE'] }))
  expect(readBurstCfg(d)).toEqual({ windowMs: 5000, maxMs: 12000 })
})

test('readBurstCfg：字符串数字认、0 认、坏值回缺省、maxMs 不低于 windowMs', () => {
  const d = join(ROOT, 'cfg-values')
  mkdirSync(d, { recursive: true })
  const cfg = (o: Record<string, unknown>) => {
    writeFileSync(join(d, 'access.json'), JSON.stringify(o))
    return readBurstCfg(d)
  }
  expect(cfg({ burstWindowMs: '3000', burstMaxMs: '9000' })).toEqual({ windowMs: 3000, maxMs: 9000 })
  expect(cfg({ burstWindowMs: 0 })).toEqual({ windowMs: 0, maxMs: 12000 })
  expect(cfg({ burstWindowMs: 'abc', burstMaxMs: null })).toEqual(DEFAULT_BURST)
  expect(cfg({ burstWindowMs: 8000, burstMaxMs: 1000 })).toEqual({ windowMs: 8000, maxMs: 8000 })
  writeFileSync(join(d, 'access.json'), '{not json')
  expect(readBurstCfg(d)).toEqual(DEFAULT_BURST)
})
