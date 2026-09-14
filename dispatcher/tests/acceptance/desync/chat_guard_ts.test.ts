// 缺陷③b（公开 dispatcher/chat_guard.ts）：humanTsOf / isDirectorInbound 纯函数契约
import { test, expect } from "bun:test";
import "./_env.ts";
import { humanTsOf, isDirectorInbound } from "../../../chat_guard.ts";

const T = Date.UTC(2026, 8, 14, 7, 0, 0); // 2026-09-14T07:00:00Z
const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, ".000Z");

test("humanTsOf: 合法 ISO 且不超未来 60s → 返回解析毫秒", () => {
  expect(humanTsOf({ human_ts: iso(T - 5000) }, T)).toBe(T - 5000);
});

test("humanTsOf: 恰好 tsMs+60s → 仍返回（上界含等号）", () => {
  expect(humanTsOf({ human_ts: iso(T + 60_000) }, T)).toBe(T + 60_000);
});

test("humanTsOf: 超过 tsMs+60s（未来时钟）→ null", () => {
  expect(humanTsOf({ human_ts: iso(T + 61_000) }, T)).toBeNull();
});

test("humanTsOf: 缺 human_ts → null", () => {
  expect(humanTsOf({ ts: iso(T) }, T)).toBeNull();
});

test("humanTsOf: human_ts 非字符串（数字）→ null", () => {
  expect(humanTsOf({ human_ts: T }, T)).toBeNull();
});

test("humanTsOf: human_ts 不可解析 → null", () => {
  expect(humanTsOf({ human_ts: "昨天下午" }, T)).toBeNull();
  expect(humanTsOf({ human_ts: "" }, T)).toBeNull();
});

test("humanTsOf: 1970 之前的负毫秒不算正数 → null", () => {
  expect(humanTsOf({ human_ts: "1969-12-31T00:00:00.000Z" }, T)).toBeNull();
});

test("humanTsOf: meta 为 null / 非对象 → null，不抛", () => {
  expect(humanTsOf(null, T)).toBeNull();
  expect(humanTsOf(undefined, T)).toBeNull();
  expect(humanTsOf("x", T)).toBeNull();
  expect(humanTsOf(42, T)).toBeNull();
});

test("humanTsOf: 不看 text 前缀与 is_bot_sender，只认 human_ts", () => {
  expect(humanTsOf({ text: "[self-initiate] x", is_bot_sender: true, human_ts: iso(T - 1000) }, T)).toBe(T - 1000);
  expect(humanTsOf({ text: "你好", is_bot_sender: false }, T)).toBeNull();
});

test("isDirectorInbound: from_username 或 sender_username 为 director → true", () => {
  expect(isDirectorInbound({ from_username: "director" })).toBe(true);
  expect(isDirectorInbound({ sender_username: "director" })).toBe(true);
});

test("isDirectorInbound: 其它用户名 / 大小写不同 → false", () => {
  expect(isDirectorInbound({ from_username: "主人" })).toBe(false);
  expect(isDirectorInbound({ from_username: "Director" })).toBe(false);
  expect(isDirectorInbound({ text: "[director] 开场" })).toBe(false);
});

test("isDirectorInbound: null / 非对象 → false，不抛", () => {
  expect(isDirectorInbound(null)).toBe(false);
  expect(isDirectorInbound(undefined)).toBe(false);
  expect(isDirectorInbound("director")).toBe(false);
});
