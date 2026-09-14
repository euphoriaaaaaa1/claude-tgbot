// 缺陷④ r7 §10.5（公开 dispatcher/chat_guard.ts）：放行账本纯函数 applyReplyOutcome(ledger, src, srcHumanMs, decision, sendOk)
// "每条用户要求最多放行 1 次发群"的行为级用例：放行 → 记账 → 同一条再来 already_used → 新一条真人消息又可放行；发送失败不记账。
import { test, expect } from "bun:test";
import { NOW, MIN } from "./_env.ts";
import { applyReplyOutcome, crossChatDecision } from "../../../chat_guard.ts";

const PRIV = "123456";
const GRP = "-1001234567890";
const H = NOW - 5 * MIN;
const decide = (used: number | null, h = H) =>
  crossChatDecision(PRIV, GRP, undefined, NOW, { userRequested: true, srcHumanMs: h, srcMentionsGroup: true, usedForHumanMs: used });
const BYPASS = decide(null);
const BLOCK = crossChatDecision(PRIV, GRP, undefined, NOW, {});

test("放行且发送成功 → 返回新对象 {[src]: srcHumanMs}，入参不变", () => {
  const ledger = {};
  const out = applyReplyOutcome(ledger, PRIV, H, BYPASS, true);
  expect(out).toEqual({ [PRIV]: H });
  expect(ledger).toEqual({});
  expect(out).not.toBe(ledger);
});

test("已有别的 src 记录 → 追加不覆盖别的键", () => {
  expect(applyReplyOutcome({ "999": 1 }, PRIV, H, BYPASS, true)).toEqual({ "999": 1, [PRIV]: H });
});

test("sendOk=false → 原样返回（不记账）", () => {
  const ledger = { "999": 1 };
  expect(applyReplyOutcome(ledger, PRIV, H, BYPASS, false)).toEqual({ "999": 1 });
});

test("decision 不是 user_request 放行（被拦 / 原判据放行）→ 原样返回", () => {
  expect(applyReplyOutcome({}, PRIV, H, BLOCK, true)).toEqual({});
  const plain = crossChatDecision(PRIV, GRP, NOW - 30_000, NOW, {}); // 群刚有人，原判据放行 bypass=null
  expect(plain.block).toBe(false);
  expect(applyReplyOutcome({}, PRIV, H, plain, true)).toEqual({});
});

test("src 为 null / srcHumanMs 为 null → 原样返回", () => {
  expect(applyReplyOutcome({}, null, H, BYPASS, true)).toEqual({});
  expect(applyReplyOutcome({}, PRIV, null, BYPASS, true)).toEqual({});
});

test("ledger 缺省形状（undefined/null）→ 不抛，按空账本处理", () => {
  expect(() => applyReplyOutcome(undefined as unknown as Record<string, number>, PRIV, H, BYPASS, true)).not.toThrow();
  expect(applyReplyOutcome(null as unknown as Record<string, number>, PRIV, H, BYPASS, true)).toEqual({ [PRIV]: H });
});

test("行为链：放行 → 记账 → 同一条要求再来 denied=already_used", () => {
  let ledger: Record<string, number> = {};
  const d1 = decide(ledger[PRIV] ?? null);
  expect(d1.bypass).toBe("user_request");
  ledger = applyReplyOutcome(ledger, PRIV, H, d1, true);
  const d2 = decide(ledger[PRIV] ?? null);
  expect([d2.block, d2.bypass, d2.userRequestDenied]).toEqual([true, null, "already_used"]);
});

test("行为链：发送失败不记账 → 同一条要求第二次仍放行", () => {
  let ledger: Record<string, number> = {};
  const d1 = decide(null);
  ledger = applyReplyOutcome(ledger, PRIV, H, d1, false);
  expect(ledger).toEqual({});
  expect(decide(ledger[PRIV] ?? null).bypass).toBe("user_request");
});

test("行为链：新一条真人消息 h2 > h 又可放行一次，再记账后再次 already_used", () => {
  let ledger: Record<string, number> = {};
  ledger = applyReplyOutcome(ledger, PRIV, H, decide(null), true);
  const h2 = H + 2 * MIN;
  const d3 = decide(ledger[PRIV] ?? null, h2);
  expect(d3.bypass).toBe("user_request");
  ledger = applyReplyOutcome(ledger, PRIV, h2, d3, true);
  expect(ledger).toEqual({ [PRIV]: h2 });
  expect(decide(ledger[PRIV] ?? null, h2).userRequestDenied).toBe("already_used");
});

test("行为链：账本是别的私聊的记录 → 本私聊不受影响仍放行", () => {
  const ledger = applyReplyOutcome({}, "777", H, BYPASS, true);
  expect(decide(ledger[PRIV] ?? null).bypass).toBe("user_request");
});
