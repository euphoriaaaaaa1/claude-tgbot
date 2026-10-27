// 缺陷③b / ④ r6（公开 dispatcher/chat_guard.ts）：sidecar 读入容错 loadInboundState、
// reply 的 src / dstActive 算法 pickSrcChat / dstActiveMs、时间前缀 buildTimePrefix（INTERFACE §4.3、§6.2、§10.5）
// r7 §4.3：buildTimePrefix 由既有 time_annotate.ts 导出（本方案不改该模块，只改第二参为全局 _human）；时区经 opts.timeZone 显式传
import { test, expect } from "bun:test";
import { NOW, MIN } from "./_env.ts";
import { loadInboundState, pickSrcChat, dstActiveMs } from "../../../chat_guard.ts";
import { buildTimePrefix } from "../../../time_annotate.ts";
const TZ = { timeZone: "Asia/Shanghai" };

const H = NOW - 3000;

test("loadInboundState: 四个保留键齐全 → 原样读入", () => {
  const s = loadInboundState({ "123": NOW, _human: H, _human_by_chat: { "123": H }, _director_by_chat: { "-100": NOW }, _mentions_group_by_chat: { "123": H } });
  expect([s._human, s._human_by_chat, s._director_by_chat, s._mentions_group_by_chat]).toEqual([H, { "123": H }, { "-100": NOW }, { "123": H }]);
});

test("loadInboundState: 文件缺失/坏 JSON 的等价输入（null / 字符串 / 数组）→ 全空", () => {
  for (const j of [null, undefined, "not json", [1, 2], 42]) {
    const s = loadInboundState(j);
    expect([j, s._human, s._human_by_chat, s._director_by_chat, s._mentions_group_by_chat]).toEqual([j, null, {}, {}, {}]);
  }
});

test("loadInboundState: _human 被写成字符串 / 负数 / NaN → null", () => {
  for (const h of ["123", -1, NaN, Infinity, true]) {
    expect([h, loadInboundState({ _human: h })._human]).toEqual([h, null]);
  }
});

test("loadInboundState: 三张表非对象 → 空表；表内非有限数的值丢弃、有限数保留", () => {
  const s = loadInboundState({ _human_by_chat: "x", _director_by_chat: [1], _mentions_group_by_chat: { a: "1", b: NaN, c: NOW, d: null } });
  expect([s._human_by_chat, s._director_by_chat, s._mentions_group_by_chat]).toEqual([{}, {}, { c: NOW }]);
});

test("loadInboundState: 只有旧格式（按 chat_id 的键）→ 保留键全空，不抛", () => {
  const s = loadInboundState({ "123": NOW, "-100": NOW - MIN });
  expect([s._human, s._human_by_chat]).toEqual([null, {}]);
});

// ---------- pickSrcChat / dstActiveMs ----------
test("pickSrcChat: 取 _human_by_chat 里 ms 最大的 chatId；表空 → null", () => {
  expect(pickSrcChat({ _human: NOW, _human_by_chat: { "-100": NOW - MIN, "123": NOW, "456": NOW - 2 * MIN }, _director_by_chat: {}, _mentions_group_by_chat: {} })).toBe("123");
  expect(pickSrcChat({ _human: null, _human_by_chat: {}, _director_by_chat: {}, _mentions_group_by_chat: {} })).toBeNull();
});

test("pickSrcChat: 只看 _human_by_chat，不看 _director_by_chat（导演注入不改 src）", () => {
  expect(pickSrcChat({ _human: H, _human_by_chat: { "123": H }, _director_by_chat: { "-100": NOW }, _mentions_group_by_chat: {} })).toBe("123");
});

test("dstActiveMs: 取 _human_by_chat[dst] 与 _director_by_chat[dst] 的较大者；都缺 → undefined", () => {
  const s = { _human: null, _human_by_chat: { "-100": NOW - 5 * MIN }, _director_by_chat: { "-100": NOW - MIN, "-200": NOW - 2 * MIN }, _mentions_group_by_chat: {} };
  expect(dstActiveMs(s, "-100")).toBe(NOW - MIN);
  expect(dstActiveMs(s, "-200")).toBe(NOW - 2 * MIN);
  expect(dstActiveMs(s, "-300")).toBeUndefined();
});

test("dstActiveMs / pickSrcChat: state 非对象或缺表 → 不抛，undefined / null", () => {
  expect(() => dstActiveMs(null as unknown as Parameters<typeof dstActiveMs>[0], "-100")).not.toThrow();
  expect(pickSrcChat({} as Parameters<typeof pickSrcChat>[0])).toBeNull();
});

// ---------- buildTimePrefix ----------
test("buildTimePrefix(tsMs, null, {timeZone}) → 只出时刻行（单行），时刻按显式时区（07:00Z → 15:00）", () => {
  const s = buildTimePrefix(NOW, null, TZ);
  const lines = s.split("\n").filter((l) => l.trim());
  expect(lines.length).toBe(1);
  expect(s).toContain("15:00");
});

test("buildTimePrefix: 有 prevTsMs（8 小时前）→ 多出间隔行，且与 chat 无关（同参两次结果逐字相同）", () => {
  const a = buildTimePrefix(NOW, NOW - 8 * 3600_000, TZ);
  const b = buildTimePrefix(NOW, NOW - 8 * 3600_000, TZ);
  expect(a).toBe(b);
  expect(a.split("\n").filter((l) => l.trim()).length).toBeGreaterThanOrEqual(2);
  expect(a).not.toBe(buildTimePrefix(NOW, null, TZ));
});

test("buildTimePrefix: 群/私聊同一时刻投递、同一全局 _human → 间隔行相同；换 prevTsMs（3 天前）→ 间隔行不同", () => {
  const prev = NOW - 8 * 3600_000;
  expect(buildTimePrefix(NOW, prev, TZ)).toBe(buildTimePrefix(NOW, prev, TZ));
  expect(buildTimePrefix(NOW, NOW - 3 * 86400_000, TZ)).not.toBe(buildTimePrefix(NOW, prev, TZ));
});

test("buildTimePrefix: 不传 opts 也不抛且返回非空串", () => {
  expect(buildTimePrefix(NOW, null).length).toBeGreaterThan(0);
});
