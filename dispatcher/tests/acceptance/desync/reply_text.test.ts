// 缺陷④ r6（公开 dispatcher/chat_guard.ts）：crossSceneHint(access) 与 replyToolDescription() 纯函数文案（§6.3、§10.5）
// 另含 §10.5 明文列出的两条守卫 grep（dispatcher.ts / worker-manager.ts 只经纯函数打标，防手写绕过）——
// 这是 INTERFACE 把源码文本列为契约的唯一两处，断言只比较数字，不把源码打进输出。
import { test, expect } from "bun:test";
import "./_env.ts";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { crossSceneHint, replyToolDescription } from "../../../chat_guard.ts";

const ACCESS = { allowFrom: ["123456"], groups: { "-1001234567890": { title: "姐妹群" } } };

test("crossSceneHint: 正常 access → 非空，含 r2/r4/r6 要求的全部子串", () => {
  const s = crossSceneHint(ACCESS);
  expect(s.length).toBeGreaterThan(0);
  for (const sub of ["私聊里聊到的内容", "永远不带进群", "明确让你去群里说", "user_requested", "其它任何情况都不得声明", "私聊里照常回"]) {
    expect([sub, s.includes(sub)]).toEqual([sub, true]);
  }
});

test("crossSceneHint: 不再含旧句（r1 '突然想到' / r4 '不要在私聊里代答' / r5 '已经在私聊里回过'）", () => {
  const s = crossSceneHint(ACCESS);
  for (const sub of ["私聊时突然想到适合在群里讲的事", "不要在私聊里代答", "已经在私聊里回过"]) {
    expect([sub, s.includes(sub)]).toEqual([sub, false]);
  }
});

test("crossSceneHint: access 非对象 / 缺 allowFrom[0] / 缺 groups 首键 → 空串，不抛", () => {
  for (const a of [null, undefined, "x", 1, {}, { allowFrom: [], groups: { "-1": {} } }, { allowFrom: ["1"], groups: {} }, { allowFrom: ["1"] }]) {
    expect([a, crossSceneHint(a)]).toEqual([a, ""]);
  }
});

test("replyToolDescription: user_requested 描述逐字", () => {
  expect(replyToolDescription()).toContain("只有用户在私聊里刚刚（10 分钟内）明确让你去群里说某件事时才置 true；其它任何情况不得声明");
});

// ---------- §10.5 守卫 grep（INTERFACE 明列）----------
const count = (file: string, needle: string) => readFileSync(join(import.meta.dir, "../../..", file), "utf8").split(needle).length - 1;

test("守卫: dispatcher.ts 恰 1 处 inboxHumanMarks( 调用，0 处手写 human_ts: / mentions_group: 字面量", () => {
  expect(count("dispatcher.ts", "inboxHumanMarks(")).toBe(1);
  expect(count("dispatcher.ts", "human_ts:")).toBe(0);
  expect(count("dispatcher.ts", "mentions_group:")).toBe(0);
});

test("守卫: worker-manager.ts 恰 1 处 applyInboundMarks( 调用", () => {
  expect(count("worker-manager.ts", "applyInboundMarks(")).toBe(1);
});

// r7 §10.5：放行账本只经 applyReplyOutcome 更新；积压投递只经 planInboxBatch 规划
test("守卫(r7): worker-plugin.ts 恰 1 处 applyReplyOutcome( 调用，0 处对 _group_request_used_ms[...] 的直接赋值", () => {
  expect(count("worker-plugin.ts", "applyReplyOutcome(")).toBe(1);
  const src = readFileSync(join(import.meta.dir, "../../..", "worker-plugin.ts"), "utf8");
  expect((src.match(/_group_request_used_ms\[[^\]]*\]\s*=[^=]/g) ?? []).length).toBe(0);
});

test("守卫(r7): worker-manager.ts 恰 1 处 planInboxBatch( 调用", () => {
  expect(count("worker-manager.ts", "planInboxBatch(")).toBe(1);
});
