// /comic_round 接线测试：无状态固定形状、计轮幂等、写失败 500、image 关、参数校验。
// 导入前把 HOME / CHANNEL_DIR / Bot API / 状态目录指向一次性目录与本机死端口，绝不碰真实服务。
import { afterAll, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

const BASE = mkdtempSync(join(tmpdir(), 'comic-round-wire-'))
const HOME = join(BASE, 'home')
const CH = join(BASE, 'channel')
const COMIC_ROOT = join(BASE, 'comic')
const BOT = 'comic-round-wire'
mkdirSync(HOME, { recursive: true })
mkdirSync(CH, { recursive: true })
process.env.HOME = HOME
process.env.CHANNEL_DIR = CH
process.env.BOT_NAME = BOT
process.env.TELEGRAM_BOT_TOKEN = '000000:FAKE-TOKEN-FOR-TEST'
process.env.DISPATCHER_TELEGRAM_API_BASE = 'http://127.0.0.1:9'
process.env.DISPATCHER_PORT = '0'
process.env.DISPATCHER_COMIC_MODE_DIR = COMIC_ROOT

const { handleComicRoundRequest, handlePhotoRoundRequest } = await import('./dispatcher.ts')
const { triggerPhotoState } = await import('./photo_mode.ts')

const CHAT = '123456789'
const statePath = join(COMIC_ROOT, BOT, `${CHAT}.json`)
const INACTIVE = { ok: true, active: false, round: 0, expires_at_ms: 0 }

afterAll(() => {
  try { chmodSync(join(COMIC_ROOT, BOT), 0o700) } catch { /* 可能没改 */ }
  rmSync(BASE, { recursive: true, force: true })
})

const call = (body: unknown, raw = false) => handleComicRoundRequest(new Request('http://127.0.0.1/comic_round', {
  method: 'POST', body: raw ? String(body) : JSON.stringify(body),
}))
const setImage = (on: boolean) => writeFileSync(join(CH, 'features.json'), JSON.stringify({ version: 1, default: true, features: { image: on } }))
const seed = () => {
  mkdirSync(join(COMIC_ROOT, BOT), { recursive: true })
  writeFileSync(statePath, JSON.stringify(triggerPhotoState(CHAT, '7', Date.now())))
}

test('无状态：200 固定形状；拍照端点同形', async () => {
  setImage(true)
  const r = await call({ chat_id: CHAT, delivery_key: 'k1' })
  expect(r.status).toBe(200)
  expect(await r.json()).toEqual(INACTIVE)
  const p = await handlePhotoRoundRequest(new Request('http://127.0.0.1/photo_round', {
    method: 'POST', body: JSON.stringify({ chat_id: CHAT, delivery_key: 'k1' }),
  }))
  expect(await p.json()).toEqual(INACTIVE)
})

test('有状态：同 key 不增轮，换 key 加一轮，数字 chat_id 也收', async () => {
  setImage(true)
  seed()
  const a = await (await call({ chat_id: CHAT, delivery_key: 'same' })).json()
  const b = await (await call({ chat_id: Number(CHAT), delivery_key: 'same' })).json()
  expect(a.round).toBe(1)
  expect(a.active).toBe(true)
  expect(b).toEqual(a)
  const c = await (await call({ chat_id: CHAT, delivery_key: 'other' })).json()
  expect(c.round).toBe(2)
  expect(JSON.parse(readFileSync(statePath, 'utf8')).rounds).toBe(2)
})

test.skipIf(process.platform === 'win32')('状态写不进：500 state_write_failed，目录权限不被放宽', async () => {
  setImage(true)
  seed()
  chmodSync(join(COMIC_ROOT, BOT), 0o500)
  try {
    const r = await call({ chat_id: CHAT, delivery_key: 'fresh-key' })
    expect(r.status).toBe(500)
    expect(await r.json()).toEqual({ ok: false, error: 'state_write_failed' })
  } finally {
    chmodSync(join(COMIC_ROOT, BOT), 0o700)
  }
})

test('image 关：删状态并返回固定形状', async () => {
  seed()
  setImage(false)
  const r = await call({ chat_id: CHAT, delivery_key: 'k9' })
  expect(await r.json()).toEqual(INACTIVE)
  expect(existsSync(statePath)).toBe(false)
  setImage(true)
})

test('参数校验：body / chat_id / delivery_key', async () => {
  const bad = async (r: Response) => ({ status: r.status, json: await r.json() })
  expect(await bad(await call('{坏', true))).toEqual({ status: 400, json: { ok: false, error: 'bad_request', detail: 'body' } })
  expect(await bad(await call({ chat_id: '../x', delivery_key: 'k' }))).toEqual({ status: 400, json: { ok: false, error: 'bad_request', detail: 'chat_id' } })
  expect(await bad(await call({ chat_id: CHAT, delivery_key: '' }))).toEqual({ status: 400, json: { ok: false, error: 'bad_request', detail: 'delivery_key' } })
  expect(await bad(await call({ chat_id: CHAT, delivery_key: 'k'.repeat(513) }))).toEqual({ status: 400, json: { ok: false, error: 'bad_request', detail: 'delivery_key' } })
})
