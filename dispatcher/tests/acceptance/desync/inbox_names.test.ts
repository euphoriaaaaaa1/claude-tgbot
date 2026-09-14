// 需求⑤ r5/r6（公开 dispatcher/inbox_names.ts）：合成消息文件名判定、陈旧阈值、pump 只调的 staleDecision
import { test, expect } from "bun:test";
import { NOW, MIN } from "./_env.ts";
import { SYNTHETIC_FILE_PREFIXES, STALE_SYNTHETIC_MS, isSyntheticName, staleDecision } from "../../../inbox_names.ts";

test("常量：六个合成前缀逐字、陈旧阈值 30 分钟", () => {
  expect(SYNTHETIC_FILE_PREFIXES).toEqual(["self-", "director-", "dm-nudge-", "moment-", "hang-", "voice-"]);
  expect(STALE_SYNTHETIC_MS).toBe(30 * MIN);
});

test("六种合成文件名都判为合成", () => {
  for (const n of ["self-1789370825832.json", "director-1789370825832.json", "dm-nudge-1.json", "moment-reply-1.json", "moment-image-1.json", "hang-1.json", "voice-1.json"]) {
    expect([n, isSyntheticName(n)]).toEqual([n, true]);
  }
});

test("真人消息文件名（数字 chat 前缀 / 负数群 id / user-moment-）不是合成", () => {
  for (const n of ["123456-1789370825832.json", "-1001234567890-1789370825832.json", "user-moment-1.json"]) {
    expect([n, isSyntheticName(n)]).toEqual([n, false]);
  }
});

test("前缀必须带连字符：selfie- / directory- / hangout- 不算", () => {
  expect(isSyntheticName("selfie-1.json")).toBe(false);
  expect(isSyntheticName("directory-1.json")).toBe(false);
  expect(isSyntheticName("hangout-1.json")).toBe(false);
});

test("空串 / 只有前缀 / 中文名 → 不抛，按 startsWith 判", () => {
  expect(isSyntheticName("")).toBe(false);
  expect(isSyntheticName("self-")).toBe(true);
  expect(isSyntheticName("陈璐璐-1.json")).toBe(false);
});

test("只看 basename 语义：带目录的路径不应误判（调用方传 basename）", () => {
  expect(isSyntheticName("inbox/self-1.json")).toBe(false);
});

// ---------- staleDecision(basename, mtimeMs, nowMs) ----------
test("staleDecision: 合成文件 31 分钟前 → drop；29 分钟前 → keep", () => {
  expect(staleDecision("self-1.json", NOW - 31 * MIN, NOW)).toBe("drop");
  expect(staleDecision("director-1.json", NOW - 29 * MIN, NOW)).toBe("keep");
});

test("staleDecision: 恰好 30 分钟 → keep（阈值不含等号：now - mtime > 阈值 才 drop）", () => {
  expect(staleDecision("moment-reply-1.json", NOW - 30 * MIN, NOW)).toBe("keep");
  expect(staleDecision("moment-reply-1.json", NOW - 30 * MIN - 1, NOW)).toBe("drop");
});

test("staleDecision: 真人消息文件再旧也 keep（停用期间积压的真人消息不丢）", () => {
  expect(staleDecision("123456-1789370825832.json", NOW - 3 * 24 * 3600_000, NOW)).toBe("keep");
  expect(staleDecision("user-moment-1.json", NOW - 3 * 24 * 3600_000, NOW)).toBe("keep");
});

test("staleDecision: mtime 非有限数（stat 失败）→ keep（旧行为照投）；未来 mtime → keep", () => {
  expect(staleDecision("self-1.json", NaN, NOW)).toBe("keep");
  expect(staleDecision("self-1.json", undefined as unknown as number, NOW)).toBe("keep");
  expect(staleDecision("self-1.json", NOW + MIN, NOW)).toBe("keep");
});
