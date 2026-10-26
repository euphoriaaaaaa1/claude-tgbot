// pub-draft 页面提示纯函数验收（B5a）。接口只允许读接口文件，不读 console.html 源码，
// 因此这里从模板里抽出内联 JS，按"行为"找出所有候选纯函数，逐一用 B5a 用例试；
// 只要有一个函数满足全部用例即通过（不依赖函数名）。
// B5b（渲染/hidden/textContent）需要真实 DOM，bun 无 DOM 且不许装依赖，见报告"覆盖缺口"。
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
      if (prop === "querySelectorAll" || prop === "getElementsByClassName"
          || prop === "getElementsByTagName") return () => [];
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

function loadCandidates(html) {
  const scripts = inlineScripts(html);
  const found = new Map();
  const attempt = (code) => {
    const names = declaredNames(code);
    if (!names.length) return;
    const body = code + "\n;return {" + names.map((n) => JSON.stringify(n) + ": typeof " + n + " === 'function' ? " + n + " : null").join(",") + "};";
    try {
      const fn = new Function("window", "document", "globalThis", "location", "fetch",
                              "localStorage", "navigator", "alert", body);
      const globals = anyStub();
      const result = fn(globals, globals, globals, globals, globals,
                        globals, globals, () => {});
      for (const [name, value] of Object.entries(result || {})) {
        if (typeof value === "function") found.set(name, value);
      }
    } catch (_e) {
      // 模板里可能有依赖真实 DOM 的初始化代码；抛错就跳过这段
    }
  };
  try {
    attempt(scripts.join("\n;\n"));
  } catch (_e) {}
  for (const code of scripts) attempt(code);
  return found;
}

const CASES = [
  ["空数组算空", [{ key: "sleep_hours", empty_hint: "H" }], { sleep_hours: [] }, ["H"]],
  ["null 算空", [{ key: "sleep_hours", empty_hint: "H" }], null, ["H"]],
  ["纯空白算空", [{ key: "sleep_hours", empty_hint: "H" }], { sleep_hours: "   " }, ["H"]],
  ["有值不提示", [{ key: "sleep_hours", empty_hint: "H" }], { sleep_hours: ["00:30-06:30"] }, []],
  ["同文案只出一次", [{ key: "a", empty_hint: "同" }, { key: "b", empty_hint: "同" }], {}, ["同"]],
  ["按字段表顺序", [{ key: "a", empty_hint: "A" }, { key: "b", empty_hint: "B" }], {}, ["A", "B"]],
];

function matches(fn) {
  for (const order of ["fields-values", "values-fields"]) {
    let ok = true;
    for (const [, fields, values, expected] of CASES) {
      const args = order === "fields-values" ? [fields, values] : [values, fields];
      let got;
      try {
        got = fn(...args);
      } catch (_e) {
        ok = false;
        break;
      }
      if (JSON.stringify(got) !== JSON.stringify(expected)) {
        ok = false;
        break;
      }
    }
    if (ok) return order;
  }
  return null;
}

test("B5a 空值提示纯函数：6 条用例全部满足", () => {
  const html = readFileSync(TEMPLATE, "utf8");
  const candidates = loadCandidates(html);
  const names = [...candidates.keys()];
  const hit = names.find((n) => matches(candidates.get(n)) !== null);
  expect(hit, "console.html 内联脚本里没找到满足 B5a 的纯函数；候选：" + names.join(",")).toBeDefined();
});

// B5b：提示条 hidden/textContent 与 input 事件需要真实 DOM。
// bun 没有 DOM，仓库也没装 jsdom/happy-dom（不引入新依赖），console.html 又不允许读实现，
// 因此这条不自动化，交人工或在允许装 DOM 依赖的环境补。
test.skip("B5b 提示条渲染与 input 事件（需 DOM，未自动化）", () => {});
