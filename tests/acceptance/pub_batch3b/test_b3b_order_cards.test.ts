// pub-batch3b · P11：卡片排序纯函数 orderCards。
// 从 console.html 抽出内联脚本，用 Proxy 桩加载（照 pub_draft/console_hints.test.ts 的先例），
// 只找行为：名字叫 orderCards（顶层函数或某个导出对象的同名方法）即算候选。
import { test, expect } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const REPO = join(import.meta.dir, "..", "..", "..");
const TEMPLATE = join(REPO, "moments", "templates", "console.html");

function anyStub() {
  const target = function () {};
  return new Proxy(target, {
    get(_t, prop) {
      if (prop === "length") return 0;
      if (prop === Symbol.iterator) return function* () {};
      if (prop === "addEventListener" || prop === "removeEventListener") return () => {};
      return anyStub();
    },
    set() { return true; },
    has() { return true; },
    apply() { return anyStub(); },
    construct() { return anyStub(); },
  });
}

function inlineScripts(html) {
  const out = [];
  const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    if (/\bsrc\s*=/.test(m[1])) continue;
    if (m[2].trim()) out.push(m[2]);
  }
  return out;
}

function declaredNames(code) {
  const names = new Set();
  for (const m of code.matchAll(/\bfunction\s+([A-Za-z_$][\w$]*)\s*\(/g)) names.add(m[1]);
  for (const m of code.matchAll(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)/g)) {
    names.add(m[1]);
  }
  return [...names];
}

function loadBindings(html) {
  const found = new Map();
  for (const code of inlineScripts(html)) {
    const names = declaredNames(code);
    if (!names.length) continue;
    const body = code + "\n;return {" + names.map((n) => JSON.stringify(n) + ": typeof " + n + " !== 'undefined' ? " + n + " : null").join(",") + "};";
    try {
      const fn = new Function("window", "document", "globalThis", "location", "fetch",
                              "localStorage", "navigator", "history", "alert", body);
      const globals = anyStub();
      const result = fn(globals, globals, globals, globals, globals, globals, globals, globals, () => {});
      for (const [name, value] of Object.entries(result || {})) found.set(name, value);
    } catch (_e) {
      // 依赖真实 DOM 的脚本块加载失败就跳过
    }
  }
  return found;
}

function findOrderCards(html) {
  const bindings = loadBindings(html);
  const direct = bindings.get("orderCards");
  if (typeof direct === "function") return direct;
  for (const value of bindings.values()) {
    if (value && typeof value === "object" && typeof value.orderCards === "function") {
      return value.orderCards.bind(value);
    }
  }
  return null;
}

const BACKENDS = [{ backend: "claude" }, { backend: "dsh" }];

function normal(id) { return { backend: "claude", bot_id: id, moved_to: "" }; }
function moved(id, to) { return { backend: "dsh", bot_id: id, moved_to: to }; }

test("P11 orderCards：正常卡在前保持相对序，迁出/切换中在后，且不改输入数组", () => {
  const orderCards = findOrderCards(readFileSync(TEMPLATE, "utf8"));
  expect(orderCards, "console.html 里没有 orderCards（顶层函数或导出对象方法）").not.toBeNull();

  const input = [normal("a"), moved("b", "claude"), moved("c", "pending"), normal("d")];
  const snapshot = JSON.stringify(input);
  const out = orderCards(input, BACKENDS);
  expect(out.map((b) => b.bot_id)).toEqual(["a", "d", "b", "c"]);
  expect(JSON.stringify(input)).toBe(snapshot);

  // 全是正常卡时顺序不变
  const all = [normal("a"), normal("b"), normal("c")];
  expect(orderCards(all, BACKENDS).map((b) => b.bot_id)).toEqual(["a", "b", "c"]);
});

