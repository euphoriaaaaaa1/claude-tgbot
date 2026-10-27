// worker-plugin（MCP stdio 服务）黑盒客户端：起子进程、走 JSON-RPC 调工具、取回工具结果。
// 只依据 .devflow/INTERFACE-h3b-delivery.md 五(13)。不读实现。
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(HERE, '..', '..', '..', 'dispatcher', 'worker-plugin.ts')

export interface McpCallResult { isError?: boolean; content?: { type: string; text?: string }[]; raw?: any }

/** 启动 worker-plugin，initialize 后调用指定工具，返回其 result（含 isError）。 */
export async function callPluginTool(opts: { env: Record<string, string>; tool: string; args: any; timeoutMs?: number }): Promise<McpCallResult> {
  const timeout = opts.timeoutMs ?? 8000
  const proc = Bun.spawn([process.execPath, 'run', PLUGIN], {
    stdin: 'pipe', stdout: 'pipe', stderr: 'pipe', env: { ...process.env, ...opts.env },
  })
  const dec = new TextDecoder()
  let buf = ''
  const responses = new Map<number, any>()
  let pendingResolve: (() => void) | null = null
  const reader = proc.stdout.getReader()
  const pump = (async () => {
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += dec.decode(value)
        let nl
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1)
          if (!line) continue
          try { const msg = JSON.parse(line); if (msg.id != null) { responses.set(msg.id, msg); pendingResolve?.() } } catch { /* 非 JSON */ }
        }
      }
    } catch { /* ignore */ }
  })()

  const send = (o: any) => { try { proc.stdin.write(JSON.stringify(o) + '\n') } catch { /* ignore */ } }
  send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'h3b-test', version: '1' } } })
  send({ jsonrpc: '2.0', method: 'notifications/initialized' })

  const waitFor = (id: number, deadline: number): Promise<any> => new Promise(resolve => {
    if (responses.has(id)) return resolve(responses.get(id))
    const t = setTimeout(() => resolve(undefined), Math.max(0, deadline - Date.now()))
    pendingResolve = () => { if (responses.has(id)) { clearTimeout(t); resolve(responses.get(id)) } }
  })

  await waitFor(1, Date.now() + timeout)
  send({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: opts.tool, arguments: opts.args } })
  const resp = await waitFor(2, Date.now() + timeout)
  try { proc.kill() } catch { /* ignore */ }
  try { proc.stdin.end() } catch { /* ignore */ }
  await pump.catch(() => { /* ignore */ })
  if (!resp) return { raw: null }
  return { ...(resp.result ?? {}), raw: resp }
}
