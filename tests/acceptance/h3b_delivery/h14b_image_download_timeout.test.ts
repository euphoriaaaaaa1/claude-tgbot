// H3b · H-14 §8 图片下载超时 —— 黑盒验收
// 只依据 .devflow/INTERFACE-h3b-delivery.md 三(8)：图片下载加超时 DOWNLOAD_TIMEOUT_MS=20000；
// 超时 → 记 download_timeout。
// 手法：子进程驱动内调用本机 dispatcher 的 /download 路由（会走 downloadToMedia），
// 让 Telegram 文件下载拖延 25s，观测请求是否在 20s 上界内封顶返回并记 download_timeout。
// bug 修复模式：当前下载无超时 → 驱动侧 22s 取消 → returned=false → 应为红。
import { test, expect } from 'bun:test'
import { runDriver } from './_driverlib.ts'

test('图片下载超时_封顶返回_记download_timeout', async () => {
  const r = await runDriver({
    scenario: 'download',
    runDownload: true,
    downloadDelayMs: 25000,      // 文件下载远超 20s 上界
    downloadAbortMs: 22000,      // 驱动侧兜底：22s 仍未返回即判"未封顶"
    waitMs: 800,
  }, 26000)
  const s = r.stderr.join('')
  expect(r.download?.returned).toBe(true)                    // 必须封顶返回，不得无限等待
  expect(r.download?.elapsed ?? 1e9).toBeLessThan(21000)     // 上界附近返回
  expect(r.download?.status).toBe(500)                       // 下载失败
  expect(s.includes('download_timeout')).toBe(true)          // 记下超时
}, 30000)
