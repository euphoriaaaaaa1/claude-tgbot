// inboundCameraMeta 退出分支：只退一种模式的词不吞掉另一种仍有效的模式（与磁盘状态一致）。
// 导入前把 HOME / CHANNEL_DIR / Bot API / 两个状态目录指向一次性目录与本机死端口，绝不碰真实服务。
import { afterAll, beforeEach, expect, test } from 'bun:test'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

const BASE = mkdtempSync(join(tmpdir(), 'camera-exit-meta-'))
const HOME = join(BASE, 'home')
const CH = join(BASE, 'channel')
const COMIC_ROOT = join(BASE, 'comic')
const PHOTO_ROOT = join(BASE, 'photo')
const BOT = 'camera-exit-meta'
mkdirSync(HOME, { recursive: true })
mkdirSync(CH, { recursive: true })
process.env.HOME = HOME
process.env.CHANNEL_DIR = CH
process.env.BOT_NAME = BOT
process.env.TELEGRAM_BOT_TOKEN = '000000:FAKE-TOKEN-FOR-TEST'
process.env.DISPATCHER_TELEGRAM_API_BASE = 'http://127.0.0.1:9'
process.env.DISPATCHER_PORT = '0'
process.env.DISPATCHER_COMIC_MODE_DIR = COMIC_ROOT
process.env.DISPATCHER_PHOTO_MODE_DIR = PHOTO_ROOT

const { inboundCameraMeta } = await import('./dispatcher.ts')
const { photoMeta, triggerPhotoState } = await import('./photo_mode.ts')

afterAll(() => rmSync(BASE, { recursive: true, force: true }))

const CHAT = '123456789'
const comicPath = join(COMIC_ROOT, BOT, `${CHAT}.json`)
const photoPath = join(PHOTO_ROOT, BOT, `${CHAT}.json`)
const NOW = Date.now()
const seed = (path: string) => {
  const st = triggerPhotoState(CHAT, '7', NOW)
  mkdirSync(join(path, '..'), { recursive: true })
  writeFileSync(path, JSON.stringify(st))
  return photoMeta(st)
}

beforeEach(() => {
  writeFileSync(join(CH, 'features.json'), JSON.stringify({ version: 1, default: true, features: { image: true } }))
  rmSync(comicPath, { force: true })
  rmSync(photoPath, { force: true })
})

test('漫画开着时发只退出拍照的词：meta 同时带 photo_mode_off 与 comic_mode，漫画状态保留', () => {
  const comic = seed(comicPath)
  for (const word of ['别拍了', '（正常拍照）', '不拍了！']) {
    const meta = inboundCameraMeta(CHAT, '8', word, NOW + 1000)
    expect([word, meta]).toEqual([word, { photo_mode_off: true, comic_mode: comic }])
    expect(existsSync(comicPath)).toBe(true)
  }
})

test('两种都开着时发只退出拍照的词：拍照状态删、漫画保留', () => {
  const comic = seed(comicPath)
  seed(photoPath)
  expect(inboundCameraMeta(CHAT, '8', '别拍了', NOW + 1000)).toEqual({ photo_mode_off: true, comic_mode: comic })
  expect(existsSync(photoPath)).toBe(false)
  expect(existsSync(comicPath)).toBe(true)
})

test('对称：拍照开着时发只退出漫画的词，带 comic_mode_off 与 photo_mode', () => {
  const photo = seed(photoPath)
  expect(inboundCameraMeta(CHAT, '8', '不画了', NOW + 1000)).toEqual({ comic_mode_off: true, photo_mode: photo })
  expect(existsSync(photoPath)).toBe(true)
})

test('"停"两个都退：只带两个 off，不再带任何模式', () => {
  seed(comicPath)
  seed(photoPath)
  expect(inboundCameraMeta(CHAT, '8', '停', NOW + 1000)).toEqual({ comic_mode_off: true, photo_mode_off: true })
  expect(existsSync(comicPath)).toBe(false)
  expect(existsSync(photoPath)).toBe(false)
})

test('漫画没开时发只退出拍照的词：只带 photo_mode_off', () => {
  expect(inboundCameraMeta(CHAT, '8', '别拍了', NOW + 1000)).toEqual({ photo_mode_off: true })
})
