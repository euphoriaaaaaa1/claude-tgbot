// 缺陷④ r6（公开 dispatcher/chat_guard.ts）：crossChatDecision 基础判据（未声明用户要求）
// 返回形状 r6：{ block, idleMin, bypass, userRequestDenied }（botSentMs/replied/userRequestExpired 已删）
import { test, expect } from "bun:test";
import { NOW, MIN } from "./_env.ts";
import { crossChatDecision, CROSS_GROUP_IDLE_MS, USER_REQUEST_WINDOW_MS, USER_REQUEST_ENABLED } from "../../../chat_guard.ts";

const PRIV = "123456";
const GRP = "-1001234567890";

test("常量：闲置阈值 10 分钟、用户要求窗口 10 分钟、放行开关默认开（★默认值）", () => {
  expect(CROSS_GROUP_IDLE_MS).toBe(10 * MIN);
  expect(USER_REQUEST_WINDOW_MS).toBe(10 * MIN);
  expect(USER_REQUEST_ENABLED).toBe(true);
});

test("私聊→群，群无记录 → 拦，idleMin=null，bypass/userRequestDenied 都是 null", () => {
  expect(crossChatDecision(PRIV, GRP, undefined, NOW)).toEqual({ block: true, idleMin: null, bypass: null, userRequestDenied: null });
  expect(crossChatDecision(PRIV, GRP, null, NOW).block).toBe(true);
});

test("私聊→群，群 11 分钟前有人 → 拦，idleMin=11", () => {
  const r = crossChatDecision(PRIV, GRP, NOW - 11 * MIN, NOW);
  expect(r.block).toBe(true);
  expect(r.idleMin).toBe(11);
});

test("私聊→群，群恰好 10 分钟前有人 → 不拦（阈值不含等号），idleMin=10", () => {
  const r = crossChatDecision(PRIV, GRP, NOW - 10 * MIN, NOW);
  expect(r.block).toBe(false);
  expect(r.idleMin).toBe(10);
});

test("私聊→群，群 30 秒前有人 → 不拦，idleMin=0", () => {
  const r = crossChatDecision(PRIV, GRP, NOW - 30_000, NOW);
  expect(r.block).toBe(false);
  expect(r.idleMin).toBe(0);
});

test("私聊→群，dstActiveMs 是 NaN / Infinity → 视为无记录 → 拦", () => {
  expect(crossChatDecision(PRIV, GRP, NaN, NOW).block).toBe(true);
  expect(crossChatDecision(PRIV, GRP, Infinity, NOW).block).toBe(true);
});

test("群→另一个群（群里没人 10 分钟）→ 不拦（src 不是私聊）", () => {
  expect(crossChatDecision("-100", GRP, undefined, NOW).block).toBe(false);
});

test("私聊→另一个私聊 → 不拦（dst 不是群）", () => {
  expect(crossChatDecision(PRIV, "654321", undefined, NOW).block).toBe(false);
});

test("src === dst（私聊回本人）→ 不拦", () => {
  expect(crossChatDecision(PRIV, PRIV, undefined, NOW).block).toBe(false);
});

test("src 为 null / 空串 / 非字符串 → 信息不足 fail-open，不拦", () => {
  expect(crossChatDecision(null, GRP, undefined, NOW).block).toBe(false);
  expect(crossChatDecision("", GRP, undefined, NOW).block).toBe(false);
  expect(crossChatDecision(123 as unknown as string, GRP, undefined, NOW).block).toBe(false);
});

test("dst 为空串 → 不拦（交给 /send 自己失败）", () => {
  expect(crossChatDecision(PRIV, "", undefined, NOW).block).toBe(false);
});

test("dst 非数字（@群名 / 中文 / 混杂 / 带空格）→ 按群且无记录 → 拦，即使给了 dstActiveMs", () => {
  for (const dst of ["@mygroup", "姐妹群", "-100abc", " -100", "-"]) {
    expect([dst, crossChatDecision(PRIV, dst, NOW - 1000, NOW).block]).toEqual([dst, true]);
  }
});

test("opts 非对象 / userRequested 非布尔 → 当作缺省，返回值与不传完全相同", () => {
  const base = crossChatDecision(PRIV, GRP, NOW - 11 * MIN, NOW);
  expect(crossChatDecision(PRIV, GRP, NOW - 11 * MIN, NOW, "yes" as unknown as object)).toEqual(base);
  expect(crossChatDecision(PRIV, GRP, NOW - 11 * MIN, NOW, { userRequested: "true" as unknown as boolean, srcHumanMs: NOW, srcMentionsGroup: true })).toEqual(base);
  expect(crossChatDecision(PRIV, GRP, NOW - 11 * MIN, NOW, { userRequested: 1 as unknown as boolean, srcHumanMs: NOW, srcMentionsGroup: true })).toEqual(base);
});

test("未声明用户要求：即使 srcHumanMs/srcMentionsGroup 都给了，也 bypass=null、userRequestDenied=null，按原判据拦", () => {
  const r = crossChatDecision(PRIV, GRP, undefined, NOW, { srcHumanMs: NOW - 2 * MIN, srcMentionsGroup: true });
  expect(r).toEqual({ block: true, idleMin: null, bypass: null, userRequestDenied: null });
});

test("返回对象没有 r5 的 replied / userRequestExpired 键", () => {
  const r = crossChatDecision(PRIV, GRP, undefined, NOW, { userRequested: true, srcHumanMs: NOW - MIN, srcMentionsGroup: true });
  expect(Object.keys(r).sort()).toEqual(["block", "bypass", "idleMin", "userRequestDenied"]);
});

test("任何怪输入都不抛", () => {
  expect(() => crossChatDecision(undefined as unknown as string, undefined as unknown as string, undefined, NaN)).not.toThrow();
  expect(() => crossChatDecision({} as unknown as string, [] as unknown as string, "x" as unknown as number, NOW)).not.toThrow();
  expect(() => crossChatDecision(PRIV, GRP, undefined, NOW, { userRequested: true, srcHumanMs: "1" as unknown as number, usedForHumanMs: {} as unknown as number })).not.toThrow();
});
