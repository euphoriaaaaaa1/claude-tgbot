// 需求⑤ r7 §10.5（公开 dispatcher/inbox_names.ts）：pump() 积压投递规划纯函数 planInboxBatch(heads)
// 输入 = 队列头部连续 file 项按序映射 {path, chatId}（chatId=null 表示读失败/坏 JSON/stale 已删）；不做 I/O。
import { test, expect } from "bun:test";
import "./_env.ts";
import { planInboxBatch } from "../../../inbox_names.ts";

const A1 = { path: "/in/a1.json", chatId: "A" };
const A2 = { path: "/in/a2.json", chatId: "A" };
const B1 = { path: "/in/b1.json", chatId: "B" };
const N1 = { path: "/in/n1.json", chatId: null };
const N2 = { path: "/in/n2.json", chatId: null };

test("[A1, null, A2, B1] → merge=[A1,A2] drop=[null项] rest=1（契约给的例子）", () => {
  expect(planInboxBatch([A1, N1, A2, B1])).toEqual({ chatId: "A", merge: [A1.path, A2.path], drop: [N1.path], rest: 1 });
});

test("[null, null] → 全 drop，chatId=null，merge 空，rest 0", () => {
  expect(planInboxBatch([N1, N2])).toEqual({ chatId: null, merge: [], drop: [N1.path, N2.path], rest: 0 });
});

test("[A1, B1] → 只合并 A1，rest=1（不同 chat 的 B1 不消费）", () => {
  expect(planInboxBatch([A1, B1])).toEqual({ chatId: "A", merge: [A1.path], drop: [], rest: 1 });
});

test("空队列 → {chatId:null, merge:[], drop:[], rest:0}", () => {
  expect(planInboxBatch([])).toEqual({ chatId: null, merge: [], drop: [], rest: 0 });
});

test("单项 → merge 该项 rest 0", () => {
  expect(planInboxBatch([A1])).toEqual({ chatId: "A", merge: [A1.path], drop: [], rest: 0 });
});

test("头部 null 先 drop，随后首个非 null 定 chatId（[null, A1, A2]）", () => {
  expect(planInboxBatch([N1, A1, A2])).toEqual({ chatId: "A", merge: [A1.path, A2.path], drop: [N1.path], rest: 0 });
});

test("遇到不同 chat 即停，后面同 chat 的也不再消费（[A1, B1, A2] → rest=2）", () => {
  expect(planInboxBatch([A1, B1, A2])).toEqual({ chatId: "A", merge: [A1.path], drop: [], rest: 2 });
});

test("停下之后的 null 也不消费（[A1, null, B1, null] → drop 只含第一个 null，rest=2）", () => {
  expect(planInboxBatch([A1, N1, B1, N2])).toEqual({ chatId: "A", merge: [A1.path], drop: [N1.path], rest: 2 });
});

test("merge 保持输入顺序（[A1, A2, A1'] 三条同 chat 全合并）", () => {
  const A3 = { path: "/in/a3.json", chatId: "A" };
  expect(planInboxBatch([A2, A1, A3]).merge).toEqual([A2.path, A1.path, A3.path]);
});

test("chatId 为空串（meta 缺 chat_id）是合法 chatId，不当 null：两条空串同批合并", () => {
  const E1 = { path: "/in/e1.json", chatId: "" };
  const E2 = { path: "/in/e2.json", chatId: "" };
  expect(planInboxBatch([E1, E2, A1])).toEqual({ chatId: "", merge: [E1.path, E2.path], drop: [], rest: 1 });
});

test("chatId 比较按字符串严格相等（'A' 与 'a' 不同批）", () => {
  expect(planInboxBatch([A1, { path: "/in/x.json", chatId: "a" }]).rest).toBe(1);
});

test("不修改入参数组与元素", () => {
  const heads = [{ ...A1 }, { ...N1 }, { ...B1 }];
  const snapshot = JSON.stringify(heads);
  planInboxBatch(heads);
  expect(JSON.stringify(heads)).toBe(snapshot);
});
