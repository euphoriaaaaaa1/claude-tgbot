// pub-comic 黑盒验收共享夹具：在 pub_outage 的「真 dispatcher 子进程 + 本机假 Telegram」之上加三样东西
//   1. 多个私聊 chat（每条用例想要互不干扰的聊天就各用一个 chat id）
//   2. 假 claude：只把 worker 收到的每一行 stdin 记进临时文件，并回 init / result 事件，用来观察「投递给 bot 的正文」
//   3. 漫画 / 拍照状态目录与时钟接缝的环境变量（全部指向临时目录）
// 零副作用：HOME / CHANNEL_DIR / 状态目录都在 Harness 的临时目录里，不连外网，不起真 bot。
import { chmodSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect } from 'bun:test'
import { Harness, PRIVATE_CHAT, sleep, startedPolling, waitFor } from '../pub_outage/_harness.ts'
export { PRIVATE_CHAT, sleep, waitFor, stopAll, API_TOKEN, REPO } from '../pub_outage/_harness.ts'
export type { Harness } from '../pub_outage/_harness.ts'

/** 本批用例用到的私聊 chat（都放进 allowFrom） */
export const CHATS = ['123456789', '123456790', '123456791', '123456792', '123456793', '123456794']

export interface ComicOpts {
  bot?: string
  imageOn?: boolean
  /** 让 worker 的 dispatcher 地址指向这里（用来造 /comic_round 失败） */
  workerDispatcherUrl?: string
  extraEnv?: Record<string, string>
  /** 起进程前再预置文件 */
  preStart?: (h: Harness) => void
  /** 用假 claude 观察投递（默认 false：用 pub_outage 的 exit 97 桩） */
  fakeWorker?: boolean
}

const FAKE_CLAUDE = `#!/bin/sh
case "$*" in *--version*) echo "2.1.100 (Claude Code)"; exit 0;; esac
sid=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--session-id" ] || [ "$prev" = "--resume" ]; then sid="$a"; fi
  prev="$a"
done
echo "{\\"type\\":\\"system\\",\\"subtype\\":\\"init\\",\\"session_id\\":\\"$sid\\",\\"model\\":\\"fake\\"}"
while IFS= read -r line; do
  printf '%s\\n' "$line" >> "__LOG__"
  echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",\\"is_error\\":false,\\"result\\":\\"\\",\\"session_id\\":\\"$sid\\",\\"num_turns\\":1,\\"duration_ms\\":1}"
done
`

export class Comic {
  constructor(public h: Harness) {}
  get comicRoot(): string { return join(this.h.base, 'comic-state') }
  get photoRoot(): string { return join(this.h.base, 'photo-state') }
  comicPath(chat: string): string { return join(this.comicRoot, this.h.bot, `${chat}.json`) }
  photoPath(chat: string): string { return join(this.photoRoot, this.h.bot, `${chat}.json`) }
  get workerLog(): string { return join(this.h.base, 'worker-stdin.log') }

  readJson(p: string): any | null { try { return JSON.parse(readFileSync(p, 'utf8')) } catch { return null } }
  has(p: string): boolean { return existsSync(p) }
  /** 状态根目录下所有文件（相对路径） */
  filesUnder(root: string): string[] {
    const out: string[] = []
    const walk = (d: string, rel: string) => {
      let es: string[] = []
      try { es = readdirSync(d, { withFileTypes: true }).map(e => (e.isDirectory() ? e.name + '/' : e.name)) } catch { return }
      for (const e of es) { if (e.endsWith('/')) walk(join(d, e), rel + e); else out.push(rel + e) }
    }
    walk(root, '')
    return out
  }

  /** worker 收到的每次投递的正文文本（按到达顺序） */
  deliveries(): string[] {
    let raw = ''
    try { raw = readFileSync(this.workerLog, 'utf8') } catch { return [] }
    const out: string[] = []
    for (const line of raw.split('\n')) {
      if (!line.trim()) continue
      try {
        const j = JSON.parse(line)
        const c = j?.message?.content ?? j?.content ?? j
        if (typeof c === 'string') out.push(c)
        else if (Array.isArray(c)) out.push(c.map((p: any) => (typeof p === 'string' ? p : p?.text ?? '')).join(''))
        else out.push(line)
      } catch { out.push(line) }
    }
    return out
  }

  /** 推一条真人私聊文字 */
  say(text: string, chat = CHATS[0]): void {
    this.h.fake.pushText({ chatId: chat, fromId: chat, text })
  }
  /** 一次性推多条（同一个 getUpdates 批里到达，便于触发连发合并） */
  burst(texts: string[], chat = CHATS[0]): void { for (const t of texts) this.say(t, chat) }

  /** 等入站件出现并返回其内容；exact=true 时正文必须与 text 完全相等，否则只要包含 */
  async inboundWith(text: string, exact = true, timeoutMs = 30000): Promise<any> {
    const item = await waitFor(() => this.h.inboxItems().find(i => {
      const t = i.data?.text
      return typeof t === 'string' && (exact ? t === text : t.includes(text))
    }), timeoutMs, `入站件正文 ${exact ? '等于' : '含'} ${text}`, 300)
    return item.data
  }
  /** 等条件成立；超时则以断言失败收场（消息写明「预期发生却没发生」的是什么） */
  async eventually(fn: () => boolean | Promise<boolean>, label: string, timeoutMs = 10000): Promise<void> {
    const end = Date.now() + timeoutMs
    let ok = false
    while (Date.now() < end) {
      try { ok = !!(await fn()) } catch { ok = false }
      if (ok) break
      await sleep(200)
    }
    expect(ok ? 'ok' : `超时仍未发生: ${label}`).toBe('ok')
  }
  /** 等某个状态文件出现并读出内容；超时按断言失败处理 */
  async stateAppears(p: string, timeoutMs = 10000): Promise<any> {
    await this.eventually(() => this.readJson(p) !== null, `状态文件出现 ${p.split('/').slice(-2).join('/')}`, timeoutMs)
    return this.readJson(p)
  }
  /** stderr 中含某片段的行 */
  logLines(part: string): string[] { return this.h.stderr().split('\n').filter(l => l.includes(part)) }
}

export async function comicHarness(o: ComicOpts = {}): Promise<Comic> {
  const extra: Record<string, string> = { CLAUDEBOT_TEST: '1', ...(o.extraEnv ?? {}) }
  const comic: { c?: Comic } = {}
  const h = await startedPolling({
    bot: o.bot ?? 'demo',
    access: { allowFrom: CHATS },
    extraEnv: extra,
    preStart: hh => {
      const c = new Comic(hh)
      comic.c = c
      mkdirSync(c.comicRoot, { recursive: true })
      mkdirSync(c.photoRoot, { recursive: true })
      extra.DISPATCHER_COMIC_MODE_DIR = c.comicRoot
      extra.DISPATCHER_PHOTO_MODE_DIR = c.photoRoot
      writeFileSync(join(hh.channel, 'features.json'), JSON.stringify({
        version: 1, default: true,
        features: Object.fromEntries(['voice', 'image', 'stickers', 'moments', 'proactive', 'life', 'relationship', 'groups']
          .map(k => [k, k === 'image' ? (o.imageOn ?? true) : true])),
      }))
      if (o.fakeWorker) {
        const dir = join(hh.base, 'fake')
        mkdirSync(dir, { recursive: true })
        const p = join(dir, 'claude')
        writeFileSync(p, FAKE_CLAUDE.replace('__LOG__', join(hh.base, 'worker-stdin.log')))
        chmodSync(p, 0o755)
        extra.CLAUDE_BIN = p
      }
      if (o.workerDispatcherUrl) { extra.TELEGRAM_DISPATCHER_URL = o.workerDispatcherUrl; extra.DISPATCHER_URL = o.workerDispatcherUrl }
      o.preStart?.(hh)
    },
  })
  return comic.c!
}

/** 等 ms 毫秒，用来「确认某事没有发生」 */
export const settle = (ms = 3000) => sleep(ms)
