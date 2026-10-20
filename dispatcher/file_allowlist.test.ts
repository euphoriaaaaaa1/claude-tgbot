// file_allowlist.ts 单测：/send files 出口白名单。
import { afterEach, describe, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'fs'
import { homedir, tmpdir } from 'os'
import { join } from 'path'
import { resolveAllowedFile } from './file_allowlist'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't-files-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

describe('resolveAllowedFile', () => {
  test('白名单根目录内的真实文件通过，返回 realpath', () => {
    const root = mkDir()
    const f = join(root, 'a.png')
    writeFileSync(f, 'x')
    const r = resolveAllowedFile(f, [root], homedir())
    expect(r).toEqual({ ok: true, path: realpathSync(f) })
  })

  test('../ 逃逸被拒', () => {
    const root = mkDir()
    const outside = mkDir()
    const f = join(outside, 'x.png')
    writeFileSync(f, 'x')
    const r = resolveAllowedFile(join(root, '..', outside.split('/').pop()!, 'x.png'), [root], homedir())
    expect(r).toEqual({ ok: false, reason: 'outside_roots' })
  })

  test('软链（文件或目录逃逸）被拒', () => {
    const root = mkDir()
    const outside = mkDir()
    const target = join(outside, 'secret.png')
    writeFileSync(target, 'x')
    const link = join(root, 'link.png')
    symlinkSync(target, link)
    expect(resolveAllowedFile(link, [root], homedir())).toEqual({ ok: false, reason: 'symlink' })

    const linkDir = join(root, 'dirlink')
    symlinkSync(outside, linkDir)
    expect(resolveAllowedFile(join(linkDir, 'secret.png'), [root], homedir()))
      .toEqual({ ok: false, reason: 'outside_roots' })
  })

  test('.env / providers.json / configs / secrets 永不通过', () => {
    const root = mkDir()
    mkdirSync(join(root, 'configs'))
    mkdirSync(join(root, 'secrets'))
    for (const rel of ['.env', '.env.local', 'providers.json', 'configs/a.png', 'secrets/b.png']) {
      const f = join(root, rel)
      writeFileSync(f, 'x')
      expect(resolveAllowedFile(f, [root], homedir()).ok).toBe(false)
    }
  })

  test('/etc/passwd、不存在的文件、空串、非字符串都被拒', () => {
    const root = mkDir()
    expect(resolveAllowedFile('/etc/passwd', [root], homedir()))
      .toEqual({ ok: false, reason: 'outside_roots' })
    expect(resolveAllowedFile(join(root, 'missing.png'), [root], homedir()))
      .toEqual({ ok: false, reason: 'not_found' })
    for (const v of ['', '   ', null, 42, undefined]) {
      expect(resolveAllowedFile(v, [root], homedir())).toEqual({ ok: false, reason: 'invalid' })
    }
  })

  test('多个根目录任一命中即可；~ 能展开', () => {
    const home = mkDir()
    const root = join(home, 'media')
    mkdirSync(root)
    const f = join(root, 'b.png')
    writeFileSync(f, 'x')
    expect(resolveAllowedFile('~/media/b.png', [join(home, 'other'), '~/media'], home))
      .toEqual({ ok: true, path: realpathSync(f) })
  })
})
