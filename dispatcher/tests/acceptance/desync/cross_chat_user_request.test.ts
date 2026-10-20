// 缺陷④ r6（公开 dispatcher/chat_guard.ts）：用户明确要求"去群里说"的放行判据（用户更正 #3）
// 四条同时成立才放行：userRequested===true、srcHumanMs 在 10 分钟窗口内（无下界）、srcMentionsGroup===true、
// usedForHumanMs !== srcHumanMs（这条要求还没发过群）。bot 是否已在私聊回过话不影响放行。
import { test, expect } from "bun:test";
import { NOW, MIN } from "./_env.ts";
import { crossChatDecision } from "../../../chat_guard.ts";

const PRIV = "123456";
const GRP = "-1001234567890";
const stale = undefined; // 群无记录：基础判据会拦
const ok = { userRequested: true, srcHumanMs: NOW - 5 * MIN, srcMentionsGroup: true };

test("四条成立（5 分钟前提到群、未用过）→ 放行 bypass=user_request，不看群闲置", () => {
  expect(crossChatDecision(PRIV, GRP, stale, NOW, ok)).toEqual({ block: false, idleMin: null, bypass: "user_request", userRequestDenied: null });
});

test("窗口边界：恰好 10 分钟 → 仍放行（★默认窗口 10 分钟）", () => {
  const r = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, srcHumanMs: NOW - 10 * MIN });
  expect([r.block, r.bypass]).toEqual([false, "user_request"]);
});

test("窗口边界：10 分钟零 1 毫秒 → 拦，denied=expired", () => {
  const r = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, srcHumanMs: NOW - 10 * MIN - 1 });
  expect(r).toEqual({ block: true, idleMin: null, bypass: null, userRequestDenied: "expired" });
});

test("时钟回拨：srcHumanMs 在未来 → 视为刚说，放行", () => {
  const r = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, srcHumanMs: NOW + 5 * MIN });
  expect([r.block, r.bypass]).toEqual([false, "user_request"]);
});

test("srcHumanMs 缺失 / null / NaN / Infinity → denied=expired，拦", () => {
  for (const s of [undefined, null, NaN, Infinity]) {
    const r = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, srcHumanMs: s as number | null });
    expect([s, r.block, r.bypass, r.userRequestDenied]).toEqual([s, true, null, "expired"]);
  }
});

test("源私聊最近一条真人消息没提到群（false / 缺省 / 字符串 'true'）→ denied=no_group_word，拦", () => {
  for (const g of [false, undefined, "true"]) {
    const r = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, srcMentionsGroup: g as boolean | undefined });
    expect([g, r.block, r.bypass, r.userRequestDenied]).toEqual([g, true, null, "no_group_word"]);
  }
});

test("同一条真人消息已放行过（usedForHumanMs === srcHumanMs）→ denied=already_used，拦", () => {
  const r = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, usedForHumanMs: ok.srcHumanMs });
  expect(r).toEqual({ block: true, idleMin: null, bypass: null, userRequestDenied: "already_used" });
});

test("usedForHumanMs 是别的时刻 / null / NaN / 缺省 → 视为未用过，放行", () => {
  for (const u of [ok.srcHumanMs - 1, null, NaN, undefined]) {
    const r = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, usedForHumanMs: u as number | null });
    expect([u, r.bypass]).toEqual([u, "user_request"]);
  }
});

test("多条不成立时 denied 取按序第一个：过期 > 无群字 > 已用过", () => {
  const a = crossChatDecision(PRIV, GRP, stale, NOW, { userRequested: true, srcHumanMs: NOW - 30 * MIN, srcMentionsGroup: false, usedForHumanMs: NOW - 30 * MIN });
  expect(a.userRequestDenied).toBe("expired");
  const b = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, srcMentionsGroup: false, usedForHumanMs: ok.srcHumanMs });
  expect(b.userRequestDenied).toBe("no_group_word");
});

test("声明成立且群本来就活跃 → 不拦，bypass 仍标 user_request，idleMin=1", () => {
  const r = crossChatDecision(PRIV, GRP, NOW - MIN, NOW, ok);
  expect(r).toEqual({ block: false, idleMin: 1, bypass: "user_request", userRequestDenied: null });
});

test("声明过期但群本来就活跃 → 不拦（原判据），bypass=null，denied=expired", () => {
  const r = crossChatDecision(PRIV, GRP, NOW - MIN, NOW, { ...ok, srcHumanMs: NOW - 30 * MIN });
  expect(r).toEqual({ block: false, idleMin: 1, bypass: null, userRequestDenied: "expired" });
});

test("userRequested=false 显式传入 → 与不传完全相同（denied 也是 null）", () => {
  const a = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, userRequested: false });
  expect(a).toEqual(crossChatDecision(PRIV, GRP, stale, NOW));
});

test("声明放行时 dst 为非数字群名 → 仍放行（放行不看 dst）", () => {
  const r = crossChatDecision(PRIV, "@group", stale, NOW, ok);
  expect([r.block, r.bypass]).toEqual([false, "user_request"]);
});

test("bot 已在私聊回过话不影响放行：r5 的 botSentMs 传了也被忽略", () => {
  const r = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, botSentMs: NOW - MIN } as unknown as Parameters<typeof crossChatDecision>[4]);
  expect([r.block, r.bypass]).toEqual([false, "user_request"]);
});

test("放行后第二次同一条消息（调用方回填 usedForHumanMs）→ 拦 already_used；换一条新真人消息 → 又能放行", () => {
  const first = crossChatDecision(PRIV, GRP, stale, NOW, ok);
  const second = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, usedForHumanMs: ok.srcHumanMs });
  const third = crossChatDecision(PRIV, GRP, stale, NOW, { ...ok, srcHumanMs: NOW - MIN, usedForHumanMs: ok.srcHumanMs });
  expect([first.bypass, second.userRequestDenied, third.bypass]).toEqual(["user_request", "already_used", "user_request"]);
});
