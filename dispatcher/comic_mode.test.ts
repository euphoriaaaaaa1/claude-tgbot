// comic_mode.ts 单测：触发/退出解析、bot 名口径、状态根、状态写（权限只收紧不放宽）。只用临时目录。
import { afterAll, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { comicModeRoot, isComicExit, isValidModeBotName, parseComicTrigger, writeComicState } from './comic_mode'
import { photoStatePath, readPhotoState, triggerPhotoState } from './photo_mode'

const tmpRoot = mkdtempSync(join(tmpdir(), 'comic-mode-unit-'))
afterAll(() => {
  try { chmodSync(join(tmpRoot, 'ro'), 0o700) } catch { /* 可能没建 */ }
  rmSync(tmpRoot, { recursive: true, force: true })
})
const posix = process.platform !== 'win32'

test('触发：整条一对同类括号，括号内去空白与末尾标点后精确匹配四个词', () => {
  for (const t of ['（漫画）', '(分镜)', '（ 四格！）', '(漫画模式。)', '  （漫画）  ', '（漫 画）', '（漫画模式 。）']) {
    expect([t, parseComicTrigger(t)]).toEqual([t, true])
  }
  for (const t of ['漫画', '/漫画', '我想看（漫画）', '（漫画）吧', '（漫画好看）', '（（漫画））', '（漫画)', '(漫画）', '好（漫画）', '（漫画\n）', '（！漫画）', '', undefined, 3]) {
    expect([t, parseComicTrigger(t)]).toEqual([t, false])
  }
})

test('退出：裸词或剥一层括号后整句匹配；句中出现不算', () => {
  for (const t of ['停', '（停）', '(不画了)', '（不画了）', '结束漫画！', '退出漫画～', '正常聊~', '正常聊…', '  正常聊  ']) {
    expect([t, isComicExit(t)]).toEqual([t, true])
  }
  for (const t of ['我们先停一下', '我还不想停，继续画', '停一下', '（（停））', '不用拍了', '', null]) {
    expect([t, isComicExit(t)]).toEqual([t, false])
  }
})

test('bot 名：与 photoBotDir 同口径（字母数字下划线短横，1 到 64 个字符）', () => {
  for (const n of ['demo-bot', 'bot2', '_x', '-x', 'a'.repeat(64)]) expect([n, isValidModeBotName(n)]).toEqual([n, true])
  for (const n of ['', 'A.b_c-1', 'demo.bot', '.hidden', '..', 'a/b', 'a b', 'a'.repeat(65), '漫画', undefined, 7]) {
    expect([n, isValidModeBotName(n)]).toEqual([n, false])
  }
  // 判为合规的名字，状态路径一定拼得出来（不抛）
  for (const n of ['_x', '-x', 'a'.repeat(64)]) expect(() => photoStatePath('/r', n, '123')).not.toThrow()
})

test('状态根：DISPATCHER_COMIC_MODE_DIR 非空绝对路径优先，否则 <CHANNEL_DIR>/.comic-mode', () => {
  const abs = join(tmpRoot, 'abs-root')
  expect(comicModeRoot('/ch', { DISPATCHER_COMIC_MODE_DIR: abs })).toBe(abs)
  expect(comicModeRoot('/ch', { DISPATCHER_COMIC_MODE_DIR: 'rel/root' })).toBe(join('/ch', '.comic-mode'))
  expect(comicModeRoot('/ch', { DISPATCHER_COMIC_MODE_DIR: '' })).toBe(join('/ch', '.comic-mode'))
  expect(comicModeRoot('/ch', {})).toBe(join('/ch', '.comic-mode'))
})

test('写状态：读回形状与拍照同口径；新建目录 0700、文件 0600', () => {
  const state = triggerPhotoState('123456789', '42', 1760000000000)
  const fresh = join(tmpRoot, 'fresh', 'demo-bot', '123456789.json')
  expect(writeComicState(fresh, state)).toBe(true)
  expect(JSON.parse(readFileSync(fresh, 'utf8'))).toEqual(state)
  expect(readPhotoState(fresh, 1760000000000 + 1000)).toEqual({ state, status: 'ok' })
  if (posix) {
    expect(statSync(fresh).mode & 0o777).toBe(0o600)
    expect(statSync(join(tmpRoot, 'fresh', 'demo-bot')).mode & 0o777).toBe(0o700)
  }
})

test.skipIf(!posix)('写状态：已有宽目录收紧；只读目录不放宽、写失败不留临时文件', () => {
  const state = triggerPhotoState('123456789', '42', 1760000000000)
  const wideDir = join(tmpRoot, 'wide')
  mkdirSync(wideDir, { mode: 0o755 })
  chmodSync(wideDir, 0o755)
  expect(writeComicState(join(wideDir, '1.json'), state)).toBe(true)
  expect(statSync(wideDir).mode & 0o777).toBe(0o700)

  const roDir = join(tmpRoot, 'ro')
  mkdirSync(roDir, { mode: 0o700 })
  chmodSync(roDir, 0o500)
  const target = join(roDir, '1.json')
  expect(writeComicState(target, state)).toBe(false)
  expect(statSync(roDir).mode & 0o777).toBe(0o500)
  expect(existsSync(target)).toBe(false)
})
