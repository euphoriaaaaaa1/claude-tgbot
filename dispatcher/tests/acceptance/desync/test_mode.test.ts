// §11 测试安全硬约定（公开 dispatcher/chat_guard.ts）：testModeCheck(env) 纯函数（worker-plugin / worker-manager 启动时调用）
// r7 §11.2 第 5 条：拒绝原因 token 全表，按序首个不满足即返回；只读传入 env，不读 process.env
import { test, expect } from "bun:test";
import { ROOT, HOME, freePort } from "./_env.ts";
import { join } from "node:path";
import { testModeCheck } from "../../../chat_guard.ts";

const good = () => ({
  CLAUDEBOTLIFE_TEST: "1", CLAUDEBOTLIFE_TEST_ROOT: ROOT, HOME,
  CHANNEL_DIR: join(ROOT, "channels", "bot2"), TELEGRAM_DISPATCHER_URL: `http://127.0.0.1:${freePort()}`,
});
const OUT = "/private/var/nowhere-desync";

test("开关未设或非 '1' → ok=true（生产零影响，任何注入缺省都不拒绝）", () => {
  expect(testModeCheck({})).toEqual({ ok: true, reason: null });
  expect(testModeCheck({ HOME: "/anywhere", TELEGRAM_DISPATCHER_URL: "http://127.0.0.1:17802" }).ok).toBe(true);
  expect(testModeCheck({ ...good(), CLAUDEBOTLIFE_TEST: "true", HOME: OUT }).ok).toBe(true);
});

test("缺 CLAUDEBOTLIFE_TEST_ROOT → missing_root；ROOT 为相对路径也是 missing_root", () => {
  expect(testModeCheck({ CLAUDEBOTLIFE_TEST: "1", HOME })).toEqual({ ok: false, reason: "missing_root" });
  expect(testModeCheck({ ...good(), CLAUDEBOTLIFE_TEST_ROOT: "tmp/x" }).reason).toBe("missing_root");
});

test("HOME 未设 / 不在 ROOT 下 → home_outside_root", () => {
  const { HOME: _h, ...noHome } = good();
  expect(testModeCheck(noHome).reason).toBe("home_outside_root");
  expect(testModeCheck({ ...good(), HOME: OUT }).reason).toBe("home_outside_root");
  expect(testModeCheck({ ...good(), HOME: join(ROOT, "..", "escape") }).reason).toBe("home_outside_root");
});

test("HOME 等于 ROOT 本身 → 通过（'等于或位于其下'）", () => {
  expect(testModeCheck({ ...good(), HOME: ROOT }).ok).toBe(true);
  expect(testModeCheck({ ...good(), HOME: ROOT + "/" }).ok).toBe(true);
});

test("CHANNEL_DIR 未设也算 → path_outside_root:CHANNEL_DIR；在 ROOT 外同样", () => {
  const { CHANNEL_DIR: _c, ...noCh } = good();
  expect(testModeCheck(noCh)).toEqual({ ok: false, reason: "path_outside_root:CHANNEL_DIR" });
  expect(testModeCheck({ ...good(), CHANNEL_DIR: "/private/var/channels/bot2" }).reason).toBe("path_outside_root:CHANNEL_DIR");
});

test("HUB_CONFIGS_DIR / DIRECTOR_CHANNELS_ROOT 仅已设且非空时检查；空串不检查", () => {
  expect(testModeCheck({ ...good(), HUB_CONFIGS_DIR: OUT }).reason).toBe("path_outside_root:HUB_CONFIGS_DIR");
  expect(testModeCheck({ ...good(), DIRECTOR_CHANNELS_ROOT: OUT }).reason).toBe("path_outside_root:DIRECTOR_CHANNELS_ROOT");
  expect(testModeCheck({ ...good(), HUB_CONFIGS_DIR: "", DIRECTOR_CHANNELS_ROOT: "" }).ok).toBe(true);
  expect(testModeCheck({ ...good(), HUB_CONFIGS_DIR: join(ROOT, "cfg") }).ok).toBe(true);
});

test("TELEGRAM_DISPATCHER_URL 命中 17802 → forbidden_port；四个生产端口与 7897/7788 都拒绝；DISPATCHER_URL 同样", () => {
  expect(testModeCheck({ ...good(), TELEGRAM_DISPATCHER_URL: "http://127.0.0.1:17802" })).toEqual({ ok: false, reason: "forbidden_port" });
  for (const p of [17801, 17802, 17803, 17804, 7897, 7788]) {
    expect([p, testModeCheck({ ...good(), TELEGRAM_DISPATCHER_URL: `http://127.0.0.1:${p}` }).reason]).toEqual([p, "forbidden_port"]);
  }
  expect(testModeCheck({ ...good(), DISPATCHER_URL: "http://127.0.0.1:17801" }).reason).toBe("forbidden_port");
});

test("URL 解析失败也算 forbidden_port", () => {
  expect(testModeCheck({ ...good(), TELEGRAM_DISPATCHER_URL: "not a url" }).reason).toBe("forbidden_port");
  expect(testModeCheck({ ...good(), DISPATCHER_URL: "://:x" }).reason).toBe("forbidden_port");
});

test("顺序：多项同时不满足取按序第一个（missing_root > home > CHANNEL_DIR > HUB > port）", () => {
  const bad = { CLAUDEBOTLIFE_TEST: "1", HOME: OUT, CHANNEL_DIR: OUT, HUB_CONFIGS_DIR: OUT, TELEGRAM_DISPATCHER_URL: "http://127.0.0.1:17801" };
  expect(testModeCheck(bad).reason).toBe("missing_root");
  expect(testModeCheck({ ...bad, CLAUDEBOTLIFE_TEST_ROOT: ROOT }).reason).toBe("home_outside_root");
  expect(testModeCheck({ ...bad, CLAUDEBOTLIFE_TEST_ROOT: ROOT, HOME }).reason).toBe("path_outside_root:CHANNEL_DIR");
  expect(testModeCheck({ ...bad, CLAUDEBOTLIFE_TEST_ROOT: ROOT, HOME, CHANNEL_DIR: join(ROOT, "c") }).reason).toBe("path_outside_root:HUB_CONFIGS_DIR");
  expect(testModeCheck({ ...good(), HUB_CONFIGS_DIR: join(ROOT, "cfg"), TELEGRAM_DISPATCHER_URL: "http://127.0.0.1:7897" }).reason).toBe("forbidden_port");
});

test("全部合规（随机端口、路径都在 ROOT 下）→ {ok:true, reason:null}", () => {
  expect(testModeCheck(good())).toEqual({ ok: true, reason: null });
});

test("env 非对象 / undefined / null → 不抛", () => {
  expect(() => testModeCheck(undefined as unknown as Record<string, string>)).not.toThrow();
  expect(() => testModeCheck(null as unknown as Record<string, string>)).not.toThrow();
});

test("只读传入 env：process.env 违规不影响传入的合规 env", () => {
  const saved = process.env.HOME;
  process.env.HOME = OUT;
  try { expect(testModeCheck(good()).ok).toBe(true); } finally { process.env.HOME = saved; }
});

test("本进程环境（_env.ts 布置的，补 CHANNEL_DIR）通过自检", () => {
  expect(testModeCheck({ ...process.env, CHANNEL_DIR: join(ROOT, "channels", "x") } as Record<string, string>).ok).toBe(true);
});
