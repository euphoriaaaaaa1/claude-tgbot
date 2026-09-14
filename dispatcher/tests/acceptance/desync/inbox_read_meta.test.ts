// 需求⑤ r7 §10.5（公开 dispatcher/inbox_names.ts）：readInboxMeta(path, nowMs) —— pump() 读队头文件的唯一入口：
// 内部先 staleDecision，'drop' → 删文件 + 返回 null；否则返回解析出的 meta；读失败/坏 JSON → null。
// 契约只写了函数名与调用位置，未写导出模块：按"与 planInboxBatch 同在 inbox_names.ts"假设，导入失败只影响本文件。
// 全部文件都在 CLAUDEBOTLIFE_TEST_ROOT 下的临时 inbox 目录，不触真实 channels。
import { test, expect } from "bun:test";
import { ROOT, NOW, MIN } from "./_env.ts";
import { existsSync, mkdirSync, utimesSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { readInboxMeta } from "../../../inbox_names.ts";

const DIR = join(ROOT, "read-meta-inbox");
mkdirSync(DIR, { recursive: true });
let n = 0;
function put(name: string, body: string, ageMs: number): string {
  const p = join(DIR, `${n++}-${name}`);
  writeFileSync(p, body);
  const t = (NOW - ageMs) / 1000;
  utimesSync(p, t, t);
  return p;
}
const META = JSON.stringify({ chat_id: "123456", text: "hi", ts: new Date(NOW).toISOString() });

test("真人消息文件、刚写入 → 返回 meta（chat_id 可读），文件保留", () => {
  const p = put("1700000000000-1.json", META, 10_000);
  const m = readInboxMeta(p, NOW);
  expect(m && String(m.chat_id)).toBe("123456");
  expect(existsSync(p)).toBe(true);
});

test("真人消息文件 3 天旧 → 仍返回 meta，不删（停用期间积压的真人消息不丢）", () => {
  const p = put("1700000000000-2.json", META, 3 * 86400_000);
  expect(readInboxMeta(p, NOW)).not.toBeNull();
  expect(existsSync(p)).toBe(true);
});

test("合成 director-*.json 31 分钟旧 → null 且文件已删除", () => {
  const p = put("director-1.json", META, 31 * MIN);
  expect(readInboxMeta(p, NOW)).toBeNull();
  expect(existsSync(p)).toBe(false);
});

test("合成 director-*.json 29 分钟旧 → 返回 meta，文件保留", () => {
  const p = put("director-2.json", META, 29 * MIN);
  expect(readInboxMeta(p, NOW)).not.toBeNull();
  expect(existsSync(p)).toBe(true);
});

test("合成文件恰 30 分钟 → keep；30 分钟零 1 秒 → drop（与 staleDecision 边界一致）", () => {
  const keep = put("director-3.json", META, 30 * MIN);
  const drop = put("director-4.json", META, 30 * MIN + 1000);
  expect(readInboxMeta(keep, NOW)).not.toBeNull();
  expect(existsSync(keep)).toBe(true);
  expect(readInboxMeta(drop, NOW)).toBeNull();
  expect(existsSync(drop)).toBe(false);
});

test("坏 JSON → null，不抛；文件不由本函数删除（删除属 pump 的 drop 集合）", () => {
  const p = put("1700000000000-3.json", "{not json", 10_000);
  expect(() => readInboxMeta(p, NOW)).not.toThrow();
  expect(readInboxMeta(p, NOW)).toBeNull();
  expect(existsSync(p)).toBe(true);
});

test("文件不存在 → null，不抛", () => {
  expect(() => readInboxMeta(join(DIR, "ghost.json"), NOW)).not.toThrow();
  expect(readInboxMeta(join(DIR, "ghost.json"), NOW)).toBeNull();
});

test("meta 缺 chat_id → 仍返回对象（由调用方映射为空串 chatId）", () => {
  const p = put("1700000000000-4.json", JSON.stringify({ text: "hi" }), 10_000);
  const m = readInboxMeta(p, NOW);
  expect(m).not.toBeNull();
  expect(String((m as Record<string, unknown>).chat_id ?? "")).toBe("");
});

test("JSON 顶层不是对象（数组/字符串）→ null", () => {
  expect(readInboxMeta(put("1700000000000-5.json", "[1,2]", 10_000), NOW)).toBeNull();
  expect(readInboxMeta(put("1700000000000-6.json", '"x"', 10_000), NOW)).toBeNull();
});
