// 缺陷④ r6 端到端（公开 dispatcher/worker-plugin.ts）：reply 工具经 MCP stdio，mock /send 在 port 0（§10.5 harness 契约）
// 安全闸（fail-closed，两道都过才拉起 worker-plugin；任一不过 → 全部用例判失败，不再拉起任何进程）：
//  A. chat_guard.ts 必须导出 testModeCheck（r6 注入点）；
//  B. 用故意违规的 env（HOME 在 ROOT 之外）拉起 worker-plugin 必须 ≤5s 内 exit 97 且 stderr 含 test_mode: refuse。
// 时间戳一律用真实 Date.now()（worker 自己读时钟），绝不用固定常量。
import { test, expect, beforeAll, afterAll } from "bun:test";
import { ROOT, HOME, MIN, deadUrl } from "./_env.ts";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const WP = join(import.meta.dir, "../../../worker-plugin.ts");
const CH = join(ROOT, "channels", "bot2");
const PRIV = "123456", GRP = "-1001234567890";
let gateErr: string | null = null;
let sends: Array<Record<string, unknown>> = [];
let mock: ReturnType<typeof Bun.serve> | undefined;

const env = (over: Record<string, string> = {}) => ({
  ...process.env, CLAUDEBOTLIFE_TEST: "1", CLAUDEBOTLIFE_TEST_ROOT: ROOT, HOME, TELEGRAM_WORKER_BOT: "bot2", CHANNEL_DIR: CH,
  TELEGRAM_DISPATCHER_URL: mock ? `http://127.0.0.1:${mock.port}` : deadUrl(), DISPATCHER_URL: mock ? `http://127.0.0.1:${mock.port}` : deadUrl(), ...over,
});

beforeAll(async () => {
  mkdirSync(join(CH, "inbox"), { recursive: true });
  writeFileSync(join(CH, "access.json"), JSON.stringify({ allowFrom: [PRIV], groups: { [GRP]: { title: "姐妹群" } } }));
  mock = Bun.serve({ port: 0, fetch: async (req) => {
    if (new URL(req.url).pathname.endsWith("/send")) { sends.push(await req.json()); return Response.json({ ok: true, message_id: sends.length }); }
    return Response.json({ ok: true });
  } });
  const mod = (await import("../../../chat_guard.ts").catch(() => ({}))) as Record<string, unknown>;
  if (typeof mod.testModeCheck !== "function") { gateErr = "注入点缺失：chat_guard.testModeCheck；拒绝拉起 worker-plugin"; return; }
  const p = Bun.spawn(["bun", "run", WP], { env: env({ HOME: "/private/var/outside-root" }), stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  const code = await Promise.race([p.exited, Bun.sleep(5000).then(() => { p.kill(); return -1; })]);
  const err = await new Response(p.stderr).text();
  if (code !== 97 || !err.includes("test_mode: refuse")) gateErr = `fail-closed 未生效（exit=${code}），拒绝继续`;
});
afterAll(() => mock?.stop(true));

function sidecar(humanByChat: Record<string, number>, mentions: Record<string, number> = {}, lastChat = PRIV) {
  const st = { _human: Math.max(...Object.values(humanByChat)), _human_by_chat: humanByChat, _director_by_chat: {}, _mentions_group_by_chat: mentions };
  writeFileSync(join(CH, ".last-inbound-ts.json"), JSON.stringify(st));
  writeFileSync(join(CH, ".last-chat-id"), lastChat);
}

type Call = (args: Record<string, unknown>) => Promise<string>;
async function withWorker(fn: (call: Call) => Promise<void>): Promise<string> {
  if (gateErr) throw new Error(gateErr);
  sends = [];
  const p = Bun.spawn(["bun", "run", WP], { env: env(), stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  const pending = new Map<number, (v: Record<string, any>) => void>();
  (async () => {
    const rd = p.stdout.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await rd.read(); if (done) break; buf += dec.decode(value);
      let i; while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        try { const m = JSON.parse(line); if (m.id != null && pending.has(m.id)) { pending.get(m.id)!(m); pending.delete(m.id); } } catch {}
      }
    }
  })();
  let id = 0;
  const rpc = (method: string, params: unknown) => new Promise<Record<string, any>>((res, rej) => {
    const my = ++id; pending.set(my, res);
    p.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: my, method, params }) + "\n"); p.stdin.flush();
    setTimeout(() => { if (pending.has(my)) { pending.delete(my); rej(new Error(`rpc timeout: ${method}`)); } }, 10_000);
  });
  try {
    await rpc("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "desync-harness", version: "0" } });
    p.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n"); p.stdin.flush();
    await fn(async (args) => { const r = await rpc("tools/call", { name: "reply", arguments: args }); return r.result?.content?.[0]?.text ?? JSON.stringify(r); });
  } finally { p.kill(); }
  return await new Response(p.stderr).text();
}

test("私聊→闲置群（无记录）→ 工具返回 blocked 文案、不调 /send、stderr cross_chat_block 行", async () => {
  const T = Date.now(); sidecar({ [PRIV]: T - 2 * MIN });
  let out = "";
  const err = await withWorker(async (call) => { out = await call({ text: "合成", chat_id: GRP }); });
  expect(out.startsWith(`blocked: 私聊内容不主动发进群（群 ${GRP} 最近 未知 分钟无人说话）`)).toBe(true);
  expect(sends.length).toBe(0);
  expect(err).toContain(`worker-plugin: cross_chat_block src=${PRIV} dst=${GRP} idle_min=null user_requested=0 denied=-`);
});

test("用户 2 分钟前在私聊提到群 + user_requested:true → 照常 /send，stderr cross_chat_user_request，无 cross_chat_warn", async () => {
  const T = Date.now(); sidecar({ [PRIV]: T - 2 * MIN }, { [PRIV]: T - 2 * MIN });
  let out = "";
  const err = await withWorker(async (call) => { out = await call({ text: "合成", chat_id: GRP, user_requested: true }); });
  expect(out).toBe("sent (id: 1)");
  expect(sends.length).toBe(1);
  expect(String(sends[0].chat_id)).toBe(GRP);
  expect(err).toContain(`worker-plugin: cross_chat_user_request src=${PRIV} dst=${GRP} priv_min=2 idle_min=null`);
  expect(err).not.toContain("cross_chat_warn");
});

test("同一条用户要求第二次发群 → 拦（denied=already_used），/send 仍只 1 次", async () => {
  const T = Date.now(); sidecar({ [PRIV]: T - 2 * MIN }, { [PRIV]: T - 2 * MIN });
  let out2 = "";
  const err = await withWorker(async (call) => { await call({ text: "合成", chat_id: GRP, user_requested: true }); out2 = await call({ text: "再说一遍", chat_id: GRP, user_requested: true }); });
  expect(out2).toBe("blocked: 不能算用户要求（用户最近没在私聊里让你去群里说，或这条要求已经发过群了）；要说就在私聊里说。");
  expect(sends.length).toBe(1);
  expect(err).toContain("user_requested=1 denied=already_used");
});

test("声明 user_requested 但用户最近私聊没提到群 → 拦（denied=no_group_word），不 /send", async () => {
  const T = Date.now(); sidecar({ [PRIV]: T - 2 * MIN });
  let out = "";
  const err = await withWorker(async (call) => { out = await call({ text: "合成", chat_id: GRP, user_requested: true }); });
  expect(out.startsWith("blocked: 不能算用户要求")).toBe(true);
  expect(sends.length).toBe(0);
  expect(err).toContain("user_requested=1 denied=no_group_word");
});

test("先在私聊回一句、再带 user_requested 去群里发 → 两次都 /send（群里发消息不耽误私聊回）", async () => {
  const T = Date.now(); sidecar({ [PRIV]: T - MIN }, { [PRIV]: T - MIN });
  const outs: string[] = [];
  await withWorker(async (call) => { outs.push(await call({ text: "私聊回你", chat_id: PRIV })); outs.push(await call({ text: "群里说", chat_id: GRP, user_requested: true })); });
  expect(outs).toEqual(["sent (id: 1)", "sent (id: 2)"]);
  expect(sends.map((s) => String(s.chat_id))).toEqual([PRIV, GRP]);
});

test("群本来活跃（1 分钟前有人）且未声明 → 照常 /send，但 stderr 打 cross_chat_warn priv_min=2 grp_min=1（残余风险计量）", async () => {
  const T = Date.now(); sidecar({ [PRIV]: T - 2 * MIN, [GRP]: T - MIN });
  let out = "";
  const err = await withWorker(async (call) => { out = await call({ text: "合成", chat_id: GRP }); });
  expect(out).toBe("sent (id: 1)");
  expect(err).toContain(`worker-plugin: cross_chat_warn src=${PRIV} dst=${GRP} priv_min=2 grp_min=1`);
});

test("未传 chat_id、lastChatId 已被同伴群消息翻成闲置群 → 同样拦，不 /send", async () => {
  const T = Date.now(); sidecar({ [PRIV]: T - 2 * MIN }, {}, GRP);
  let out = "";
  await withWorker(async (call) => { out = await call({ text: "合成" }); });
  expect(out.startsWith("blocked: 私聊内容不主动发进群")).toBe(true);
  expect(sends.length).toBe(0);
});
