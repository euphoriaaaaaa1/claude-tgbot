// 缺陷③b r6（公开 dispatcher/chat_guard.ts）：inboxHumanMarks（dispatcher 写 inbox 的唯一真人打标点）
// 与 applyInboundMarks（sidecar 四个保留键的更新规则），INTERFACE §4.1 / §4.3 / §10.5
import { test, expect } from "bun:test";
import { NOW, MIN, isoZ } from "./_env.ts";
import { inboxHumanMarks, applyInboundMarks } from "../../../chat_guard.ts";

const SEC = Math.floor((NOW - 5000) / 1000);
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$/;

test("inboxHumanMarks: 真人非合成、正文不含'群' → 只有 human_ts（= message.date 的 UTC，.000Z）", () => {
  expect(inboxHumanMarks("你好呀", false, false, SEC, NOW)).toEqual({ human_ts: isoZ(SEC * 1000) });
  expect(ISO_RE.test(inboxHumanMarks("x", false, false, SEC, NOW).human_ts!)).toBe(true);
});

test("inboxHumanMarks: 正文含'群' → 多 mentions_group:true", () => {
  expect(inboxHumanMarks("你去群里说一声", false, false, SEC, NOW)).toEqual({ human_ts: isoZ(SEC * 1000), mentions_group: true });
});

test("inboxHumanMarks: 只有前导 @mention 里带'群'字 → 去掉后不含 → 无 mentions_group 键", () => {
  const r = inboxHumanMarks("@群聊小助手 你好", false, false, SEC, NOW);
  expect("mentions_group" in r).toBe(false);
  expect(inboxHumanMarks("@bot2 去群里说", false, false, SEC, NOW).mentions_group).toBe(true);
});

test("inboxHumanMarks: 不含'群'时 mentions_group 键不存在（不是 false）", () => {
  expect("mentions_group" in inboxHumanMarks("嗯", false, false, SEC, NOW)).toBe(false);
});

test("inboxHumanMarks: bot 发送者 / synthetic → 返回 {}（即使正文含'群'）", () => {
  expect(inboxHumanMarks("群里见", true, false, SEC, NOW)).toEqual({});
  expect(inboxHumanMarks("群里见", false, true, SEC, NOW)).toEqual({});
});

test("inboxHumanMarks: dateSec 非有限正数（NaN/0/-1/undefined）→ human_ts = nowMs", () => {
  for (const d of [NaN, 0, -1, undefined]) {
    expect([d, inboxHumanMarks("嗯", false, false, d as number, NOW).human_ts]).toEqual([d, isoZ(NOW)]);
  }
});

test("inboxHumanMarks: text 非字符串 → 当空串（有 human_ts、无 mentions_group），不抛", () => {
  for (const t of [null, undefined, 42, { a: 1 }]) {
    const r = inboxHumanMarks(t as unknown as string, false, false, SEC, NOW);
    expect([t, r.human_ts, "mentions_group" in r]).toEqual([t, isoZ(SEC * 1000), false]);
  }
});

// ---------- applyInboundMarks ----------
const EMPTY = { _human: null, _human_by_chat: {}, _director_by_chat: {}, _mentions_group_by_chat: {} };
const H = NOW - 3000;
const humanMeta = { from_username: "主人", human_ts: isoZ(H) };

test("applyInboundMarks: 真人消息 → _human 与 _human_by_chat[chat] 都写为 human_ts；返回新对象不改入参", () => {
  const s = applyInboundMarks(EMPTY, "123", humanMeta, NOW);
  expect(s._human).toBe(H);
  expect(s._human_by_chat).toEqual({ "123": H });
  expect(s._director_by_chat).toEqual({});
  expect(EMPTY._human).toBeNull();
});

test("applyInboundMarks: 更早的 human_ts 不倒退 _human，但比该 chat 记录新时仍写该 chat", () => {
  const prev = { ...EMPTY, _human: NOW, _human_by_chat: { "999": NOW } };
  const s = applyInboundMarks(prev, "123", humanMeta, NOW);
  expect(s._human).toBe(NOW);
  expect(s._human_by_chat).toEqual({ "999": NOW, "123": H });
});

test("applyInboundMarks: [director] 带 human_ts → 三张表都更新；不带 → 只更新 _director_by_chat", () => {
  const a = applyInboundMarks(EMPTY, "-100", { from_username: "director", human_ts: isoZ(H) }, NOW);
  expect([a._human, a._human_by_chat["-100"], a._director_by_chat["-100"]]).toEqual([H, H, NOW]);
  const b = applyInboundMarks(EMPTY, "-100", { from_username: "director", text: "[director] 开场" }, NOW);
  expect([b._human, b._human_by_chat, b._director_by_chat]).toEqual([null, {}, { "-100": NOW }]);
});

test("applyInboundMarks: [self-initiate] / peer / moment（无 human_ts、非 director）→ 四张表原样", () => {
  const prev = { _human: H, _human_by_chat: { "123": H }, _director_by_chat: {}, _mentions_group_by_chat: {} };
  for (const m of [{ from_username: "bot2", text: "[self-initiate] x" }, { from_username: "bot3", text: "[peer-inbound] y" }, { text: "[moment] z" }]) {
    expect(applyInboundMarks(prev, "123", m, NOW)).toEqual(prev);
  }
});

test("applyInboundMarks: mentions_group:true + human_ts → _mentions_group_by_chat[chat]=human_ts；无 human_ts 则不写", () => {
  const a = applyInboundMarks(EMPTY, "123", { ...humanMeta, mentions_group: true }, NOW);
  expect(a._mentions_group_by_chat).toEqual({ "123": H });
  expect(a._mentions_group_by_chat["123"]).toBe(a._human_by_chat["123"]);
  const b = applyInboundMarks(EMPTY, "123", { text: "[self-initiate] 群", mentions_group: true }, NOW);
  expect(b._mentions_group_by_chat).toEqual({});
});

test("applyInboundMarks: 后来一条不含'群'的真人消息 → _human_by_chat 前进而 _mentions_group_by_chat 停在旧值", () => {
  const a = applyInboundMarks(EMPTY, "123", { ...humanMeta, mentions_group: true }, NOW);
  const b = applyInboundMarks(a, "123", { from_username: "主人", human_ts: isoZ(H + 1000) }, NOW);
  expect([b._human_by_chat["123"], b._mentions_group_by_chat["123"]]).toEqual([H + 1000, H]);
});

test("applyInboundMarks: human_ts 超过 tsMs+60s（未来时钟）→ 丢弃，四张表不动", () => {
  const s = applyInboundMarks(EMPTY, "123", { human_ts: isoZ(NOW + 61_000), mentions_group: true }, NOW);
  expect(s).toEqual(EMPTY);
});

test("applyInboundMarks: state 缺键 / 非对象值 → 当空表处理，不抛", () => {
  const s = applyInboundMarks({} as typeof EMPTY, "123", humanMeta, NOW);
  expect([s._human, s._human_by_chat["123"]]).toEqual([H, H]);
  expect(() => applyInboundMarks({ _human: "x", _human_by_chat: 5 } as unknown as typeof EMPTY, "123", humanMeta, NOW)).not.toThrow();
});
