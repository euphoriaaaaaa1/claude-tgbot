// desync 验收（bun）运行器职责（INTERFACE §11.4）：进程入口设测试模式，并布置纵深隔离。
// - CLAUDEBOTLIFE_TEST=1 + CLAUDEBOTLIFE_TEST_ROOT=<mkdtemp>；HOME 在其下
// - PATH 前置拒绝桩：tmux/launchctl/pkill/claude/schtasks → stderr "test_mode: real <名> called"，exit 97
// - 假服务一律 port 0；绝不连 17801-17804；退出时只删 ROOT
import { chmodSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const ROOT = mkdtempSync(join(tmpdir(), "desync-ts-"));
export const HOME = join(ROOT, "home");
export const STUB = join(ROOT, "stub");
export const FORBIDDEN_PORTS = [17801, 17802, 17803, 17804, 7897, 7788];
mkdirSync(HOME);
mkdirSync(STUB);
for (const n of ["tmux", "launchctl", "pkill", "claude", "schtasks"]) {
  const p = join(STUB, n);
  writeFileSync(p, `#!/bin/sh\necho "$0 $@" >> "${ROOT}/calls.log"\necho "test_mode: real ${n} called" >&2\nexit 97\n`);
  chmodSync(p, 0o755);
}
process.env.CLAUDEBOTLIFE_TEST = "1";
process.env.CLAUDEBOTLIFE_TEST_ROOT = ROOT;
process.env.HOME = HOME;
process.env.PATH = `${STUB}:${process.env.PATH ?? ""}`;
process.on("exit", () => rmSync(ROOT, { recursive: true, force: true }));
// bun test 结束时 exit 钩子不一定触发：启动时顺手清掉上次残留的同前缀目录（只删 desync-ts-*，绝不碰别的）
for (const n of readdirSync(tmpdir())) {
  const p = join(tmpdir(), n);
  if (n.startsWith("desync-ts-") && p !== ROOT) rmSync(p, { recursive: true, force: true });
}

/** 本机随机空闲端口（永不返回生产/代理端口）。 */
export function freePort(): number {
  for (;;) {
    const s = Bun.serve({ port: 0, fetch: () => new Response("") });
    const p = s.port;
    s.stop(true);
    if (!FORBIDDEN_PORTS.includes(p)) return p;
  }
}

/** 无人监听的本机地址：任何漏网请求只会连接失败，绝不落到生产端口。 */
export function deadUrl(): string {
  return `http://127.0.0.1:${freePort()}`;
}
process.env.TELEGRAM_DISPATCHER_URL = deadUrl();
process.env.DISPATCHER_URL = process.env.TELEGRAM_DISPATCHER_URL;

export const NOW = Date.UTC(2026, 8, 14, 7, 0, 0); // 2026-09-14T07:00:00Z
export const MIN = 60_000;
export const isoZ = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, ".000Z");
