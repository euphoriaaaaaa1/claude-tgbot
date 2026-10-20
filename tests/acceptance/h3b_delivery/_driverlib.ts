// 子进程驱动客户端：把场景 spec 交给 _driver.ts 跑，读回观察结果。
// 用 process.execPath（bun 本体）启动子进程，确保与测试同版本。
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const DRIVER = join(HERE, '_driver.ts')

export interface DriverResult {
  tgMethods: string[]
  transcribe: { path: string; body: string }[]
  inboxFiles: string[]
  stderr: string[]
  events: any[]
  download?: { returned: boolean; elapsed: number; status: number; body: any }
}

export async function runDriver(spec: Record<string, unknown>, timeoutMs = 30000): Promise<DriverResult> {
  const dir = mkdtempSync(join(tmpdir(), 'h3b-drv-spec-'))
  const specPath = join(dir, 'spec.json')
  const outPath = join(dir, 'out.json')
  writeFileSync(specPath, JSON.stringify(spec))
  const proc = Bun.spawn([process.execPath, DRIVER, specPath, outPath], {
    stdout: 'ignore', stderr: 'ignore', env: { ...process.env },
  })
  const killer = setTimeout(() => { try { proc.kill() } catch { /* ignore */ } }, timeoutMs)
  await proc.exited
  clearTimeout(killer)
  if (!existsSync(outPath)) return { tgMethods: [], transcribe: [], inboxFiles: [], stderr: [], events: [] }
  try { return JSON.parse(readFileSync(outPath, 'utf8')) as DriverResult }
  catch { return { tgMethods: [], transcribe: [], inboxFiles: [], stderr: [], events: [] } }
}

// ─── Telegram 更新构造 ───────────────────────────────────────────────────
export const OWNER = '123456789'
export const NONOWNER = '100002'

let seq = 1
export function voiceUpdate(opts: { chatId: string; fromId: string; chatType?: string; messageId?: number }): any {
  return {
    update_id: seq++,
    message: {
      message_id: opts.messageId ?? seq,
      from: { id: Number(opts.fromId), is_bot: false, first_name: 'U' },
      chat: { id: Number(opts.chatId), type: opts.chatType ?? 'private' },
      date: Math.floor(Date.now() / 1000),
      voice: { file_id: 'VF1', file_unique_id: 'u1', duration: 1, mime_type: 'audio/ogg', file_size: 50 },
    },
  }
}
export function textUpdate(opts: { chatId: string; fromId: string; text: string; chatType?: string; messageId?: number }): any {
  return {
    update_id: seq++,
    message: {
      message_id: opts.messageId ?? seq,
      from: { id: Number(opts.fromId), is_bot: false, first_name: 'U' },
      chat: { id: Number(opts.chatId), type: opts.chatType ?? 'private' },
      date: Math.floor(Date.now() / 1000),
      text: opts.text,
    },
  }
}
export function photoUpdate(opts: { chatId: string; fromId: string; chatType?: string; messageId?: number }): any {
  return {
    update_id: seq++,
    message: {
      message_id: opts.messageId ?? seq,
      from: { id: Number(opts.fromId), is_bot: false, first_name: 'U' },
      chat: { id: Number(opts.chatId), type: opts.chatType ?? 'private' },
      date: Math.floor(Date.now() / 1000),
      photo: [{ file_id: 'PH1', file_unique_id: 'pu1', width: 100, height: 100, file_size: 40 }],
      caption: '一张图',
    },
  }
}
