// /photo_round 接线测试：无活跃拍照状态时也必须返回 round/expires_at_ms（INTERFACE 契约）。
// 导入前把 HOME / CHANNEL_DIR / Bot API 指向一次性目录与本机死端口，绝不碰真实服务。
import { afterAll, describe, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

const BASE = mkdtempSync(join(tmpdir(), 'photo-round-wire-'))
const HOME = join(BASE, 'home')
const CH = join(BASE, 'channel')
mkdirSync(HOME, { recursive: true })
mkdirSync(CH, { recursive: true })
process.env.HOME = HOME
process.env.CHANNEL_DIR = CH
process.env.BOT_NAME = 'photo-round-wire'
process.env.TELEGRAM_BOT_TOKEN = '000000:FAKE-TOKEN-FOR-TEST'
process.env.DISPATCHER_TELEGRAM_API_BASE = 'http://127.0.0.1:9'
process.env.DISPATCHER_PORT = '0'

const { handlePhotoRoundRequest } = await import('./dispatcher.ts')

afterAll(() => rmSync(BASE, { recursive: true, force: true }))

describe('/photo_round 无活跃状态', () => {
  test('200 恒含 round 与 expires_at_ms', async () => {
    const res = await handlePhotoRoundRequest(new Request('http://127.0.0.1/photo_round', {
      method: 'POST',
      body: JSON.stringify({ chat_id: '-100123', delivery_key: 'k1' }),
    }))
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ ok: true, active: false, round: 0, expires_at_ms: 0 })
  })
})
