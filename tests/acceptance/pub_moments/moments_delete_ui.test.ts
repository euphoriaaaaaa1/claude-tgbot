// 朋友圈页（moments/templates/moments.html）删除动态的界面逻辑：不装库、不开浏览器、不起服务。
// 依据：删除前先弹确认（文案沿用私有版逐字）；点取消不发请求、页面不变；确认后发 DELETE /api/moment/<编号>
// （JSON 头、请求体 {}，接口文件 1.7）；成功后卡片从页面去掉；失败时弹「删除失败：<返回的 error>」、卡片保留、按钮恢复可点。
// 做法：取模板里的内联脚本放进干净上下文执行；页面元素一律换成"万能假元素"（任何选择器都查得到、任何属性都能读写并留痕，
// 卡片与按钮上能读到的编号一律是 42），再逐个触发脚本挂上的点击处理，找出弹出删除确认文案的那一个。
// 不依赖函数名、类名与绑定方式（addEventListener、事件委托、onclick 赋值、内联 onclick 调全局函数都认）。
// 模板读不到时每个用例各自失败。PMOM_UI_DEBUG=1 时把每次试触发的结果打到标准错误。
import { test } from "bun:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import vm from "node:vm";

const TPL = process.env.PMOM_MOMENTS_TEMPLATE
  || join(resolve(import.meta.dir, "..", "..", ".."), "moments", "templates", "moments.html");
const DEL_TEXT = "删除这条动态？下面的评论和点赞会一起删掉，删了找不回。";
const MID = "42";
const DEBUG = process.env.PMOM_UI_DEBUG === "1";
const OUTER_SET_TIMEOUT = setTimeout;
const settle = async (n = 12) => { for (let i = 0; i < n; i++) await new Promise((r) => OUTER_SET_TIMEOUT(r, 0)); };

function pageScript(): string {
  const html = readFileSync(TPL, "utf8");
  const blocks = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)]
    .filter((m) => !/\bsrc\s*=/.test(m[1]) && !/type\s*=\s*["']?(application\/(ld\+)?json|text\/template)/i.test(m[1]))
    .map((m) => m[2]);
  assert.ok(blocks.length > 0, "模板里没有内联脚本");
  return blocks.join("\n;\n").replace(/\{\{[\s\S]*?\}\}/g, "null").replace(/\{%[\s\S]*?%\}/g, "");
}

function makeWorld(): any {
  const W: any = {
    listeners: [], mut: [], fetches: [], confirms: [], alerts: [], errors: [], initErrors: [], reloads: 0,
    disabled: new Set(), sels: new Set(), dsels: new Set(), policy: null, confirmAnswer: false, timers: new Set(),
    timerBudget: 3000, respond: () => ({ status: 200, body: {} }),
  };
  const rec = (...m: any[]) => W.mut.push(m);
  // 只有"点击目标"及从它查出来的元素受触发方式约束：only=目标只匹配哪个选择器（'*' 全匹配），except=排除哪些选择器/数据键
  const derived = (name: any) => W.policy !== null && String(name).startsWith("点击目标");
  const match = (self: any, name: any, sel: any) => {
    if (!derived(name)) return true;
    const key = "sel:" + String(sel);
    W.dsels.add(key);
    if (W.policy.target === self) {
      W.sels.add(String(sel));
      if (!(W.policy.only === "*" || W.policy.only === String(sel))) return false;
    }
    return !W.policy.except.has(key);
  };
  const keyed = (name: any, kind: string, k: any, hit: any, miss: any) => {
    if (derived(name)) {
      W.dsels.add(kind + ":" + k);
      if (W.policy.except.has(kind + ":" + k)) return miss;
    }
    return /id|mid|moment/i.test(String(k)) ? hit : miss;
  };
  function fake(name: string, extra?: Record<string, any>, prim?: any): any {
    const props = new Map<any, any>(Object.entries(extra || {}));
    const kids = new Map<string, any>();
    let self: any;
    const child = (k: string) => { if (!kids.has(k)) kids.set(k, fake(name + " > " + k)); return kids.get(k); };
    const setDisabled = (v: boolean) => { if (v) W.disabled.add(name); else W.disabled.delete(name); };
    const dataset = new Proxy({}, {
      get: (_t, k) => (typeof k === "string" ? keyed(name, "data", k, MID, undefined) : undefined),
      set: (_t, k, v) => { rec("dataset", name, String(k), v); return true; },
    });
    const styleVals = new Map<any, any>();
    const style = new Proxy({}, {
      get: (_t, k) => (k === "setProperty" ? (a: any, b: any) => { styleVals.set(a, b); rec("style", name, a, b); }
        : k === "removeProperty" ? (a: any) => { styleVals.delete(a); rec("style", name, a, ""); }
          : (styleVals.has(k) ? styleVals.get(k) : "")),
      set: (_t, k, v) => { styleVals.set(k, v); rec("style", name, String(k), v); return true; },
    });
    const classList = {
      add: (...c: any[]) => rec("class+", name, ...c), remove: (...c: any[]) => rec("class-", name, ...c),
      toggle: (c: any, f: any) => { rec("class~", name, c, f); return !!f; }, contains: () => false,
      replace: (a: any, b: any) => rec("class=", name, a, b),
    };
    const methods: Record<string, any> = {
      addEventListener: (type: any, fn: any) => W.listeners.push({ el: self, name, type: String(type), fn }),
      removeEventListener() {}, dispatchEvent: () => true,
      remove: () => rec("remove", name),
      removeChild: (c: any) => { rec("removeChild", name); return c; },
      replaceWith: () => rec("replaceWith", name),
      replaceChildren: () => rec("replaceChildren", name),
      appendChild: (c: any) => { rec("append", name); return c; }, append: () => rec("append", name),
      prepend: () => rec("prepend", name),
      insertBefore: (c: any) => { rec("insert", name); return c; }, after: () => rec("after", name),
      before: () => rec("before", name),
      insertAdjacentElement: (_p: any, c: any) => { rec("insertAdj", name); return c; },
      insertAdjacentHTML: () => rec("insertAdjHTML", name), insertAdjacentText: () => rec("insertAdjText", name),
      closest: (sel: any) => (match(self, name, sel) ? child("closest(" + sel + ")") : null),
      matches: (sel: any) => match(self, name, sel),
      contains: () => true,
      querySelector: (sel: any) => child("q(" + sel + ")"),
      querySelectorAll: (sel: any) => [child("qa(" + sel + ")")],
      getElementsByClassName: (c: any) => [child("qa(." + c + ")")],
      getElementsByTagName: (t: any) => [child("qa(" + t + ")")],
      getElementById: (id: any) => child("#" + id),
      getAttribute: (n: any) => keyed(name, "attr", String(n), MID, null),
      setAttribute: (n: any, v: any) => { rec("attr", name, String(n), v); if (String(n) === "disabled") setDisabled(true); },
      removeAttribute: (n: any) => { rec("attr-", name, String(n)); if (String(n) === "disabled") setDisabled(false); },
      toggleAttribute: (n: any, f: any) => {
        rec("attr~", name, String(n), f);
        if (String(n) === "disabled") setDisabled(f === undefined ? !W.disabled.has(name) : !!f);
        return !!f;
      },
      hasAttribute: () => false,
      focus() {}, blur() {}, click() {}, scrollIntoView() {}, select() {}, showModal() {}, show() {}, close() {},
      reset() {}, submit() {},
      getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 }),
      cloneNode: () => fake(name + " clone"),
      animate: () => ({ finished: Promise.resolve(), cancel() {}, onfinish: null }),
      createElement: (tag: any) => fake("<" + tag + ">"),
      createTextNode: () => fake("text"), createDocumentFragment: () => fake("fragment"),
    };
    self = new Proxy(function () {}, {
      get(_t, k) {
        if (k === Symbol.toPrimitive) {
          return (hint: string) => (prim === undefined ? (hint === "number" ? 0 : "") : (hint === "number" ? Number(prim) : prim));
        }
        if (k === Symbol.iterator) return function* () {};
        if (typeof k === "symbol" || k === "then" || k === "toJSON") return undefined;
        if (props.has(k)) return props.get(k);
        if (Object.prototype.hasOwnProperty.call(methods, k)) return methods[k as string];
        switch (k) {
          case "dataset": return dataset;
          case "style": return style;
          case "classList": return classList;
          case "disabled": return W.disabled.has(name);
          case "hidden": case "checked": case "open": return false;
          case "textContent": case "innerText": case "innerHTML": case "outerHTML": case "value": case "className":
          case "id": case "src": case "href": case "nodeValue": return "";
          case "length": case "offsetHeight": case "offsetWidth": case "scrollHeight": case "scrollTop":
          case "clientHeight": case "clientWidth": case "childElementCount": return 0;
          case "nodeType": return 1;
          case "children": case "childNodes": case "files": return [];
          case "tagName": case "nodeName": return "DIV";
          case "toString": return () => (prim === undefined ? "" : String(prim));
          case "valueOf": return () => (prim === undefined ? "" : prim);
        }
        return child(k as string);
      },
      set(_t, k, v) {
        props.set(k, v);
        rec("set", name, String(k), v);
        if (k === "disabled") setDisabled(!!v);
        return true;
      },
      apply() { return fake(name + "()"); },
      construct() { return fake("new " + name); },
    });
    return self;
  }
  const timer = (fn: any, ms?: any, ...a: any[]) => {
    if (typeof fn !== "function" || Number(ms) > 1000 || W.timerBudget-- <= 0) return 0;
    const h = OUTER_SET_TIMEOUT(() => {
      W.timers.delete(h);
      try { fn(...a); } catch (e: any) { W.errors.push("定时回调出错：" + (e && e.message)); }
    }, 0);
    W.timers.add(h);
    return h;
  };
  const resp = (status: number, body: any): any => ({
    ok: status >= 200 && status < 300, status, statusText: String(status), redirected: false, url: "",
    headers: new Headers({ "content-type": "application/json" }), json: async () => body,
    text: async () => JSON.stringify(body), clone() { return resp(status, body); },
  });
  const storage = () => {
    const m = new Map<string, string>();
    return {
      getItem: (k: string) => (m.has(k) ? m.get(k) : null), setItem: (k: string, v: any) => { m.set(k, String(v)); },
      removeItem: (k: string) => { m.delete(k); }, clear: () => m.clear(), key: () => null, length: 0,
    };
  };
  const Obs = class { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } };
  const anyInstance = class { static [Symbol.hasInstance]() { return true; } };
  const doc = fake("document", { readyState: "complete", cookie: "", referrer: "", visibilityState: "visible", hidden: false });
  const sb: any = {
    document: doc,
    console: { log() {}, info() {}, debug() {}, warn() {}, error: (...a: any[]) => W.errors.push("console.error：" + a.join(" ")) },
    location: {
      href: "http://localhost/moments", origin: "http://localhost", protocol: "http:", host: "localhost",
      hostname: "localhost", pathname: "/moments", search: "", hash: "",
      reload: () => { W.reloads++; }, assign: () => { W.reloads++; }, replace: () => { W.reloads++; },
    },
    history: { pushState() {}, replaceState() {}, back() {}, state: null },
    navigator: { userAgent: "bun", language: "zh-CN", clipboard: { writeText: async () => {} } },
    localStorage: storage(), sessionStorage: storage(),
    fetch: (url: any, init: any) => {
      const i = init || {};
      W.fetches.push({ url: String(url), init: i });
      const r = W.respond(String(url), i);
      return Promise.resolve(resp(r.status, r.body));
    },
    confirm: (m: any) => { W.confirms.push(String(m)); return W.confirmAnswer; },
    alert: (m: any) => { W.alerts.push(String(m)); },
    prompt: () => null,
    setTimeout: timer, clearTimeout: (h: any) => { clearTimeout(h); W.timers.delete(h); },
    setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: (fn: any) => timer(fn, 0, 0), cancelAnimationFrame() {},
    requestIdleCallback: (fn: any) => timer(fn, 0, { timeRemaining: () => 0, didTimeout: false }),
    queueMicrotask, structuredClone, URL, URLSearchParams, TextEncoder, TextDecoder, AbortController, Headers,
    FormData, Blob, atob, btoa,
    performance: { now: () => 0 },
    IntersectionObserver: Obs, ResizeObserver: Obs, MutationObserver: Obs,
    Image: function () { return fake("img"); }, FileReader: function () { return fake("FileReader"); },
    Event: class { type: any; constructor(t: any, o?: any) { this.type = t; Object.assign(this, o || {}); } },
    CustomEvent: class { type: any; constructor(t: any, o?: any) { this.type = t; Object.assign(this, o || {}); } },
    HTMLElement: anyInstance, Element: anyInstance, Node: anyInstance, HTMLInputElement: anyInstance,
    HTMLButtonElement: anyInstance, HTMLImageElement: anyInstance, HTMLTextAreaElement: anyInstance,
    getComputedStyle: () => fake("computed"),
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
    scrollTo() {}, scrollBy() {}, innerWidth: 1280, innerHeight: 800, devicePixelRatio: 1, scrollY: 0, pageYOffset: 0,
    addEventListener: (type: any, fn: any) => W.listeners.push({ el: "window", name: "window", type: String(type), fn }),
    removeEventListener() {}, dispatchEvent: () => true,
  };
  sb.window = sb; sb.self = sb; sb.top = sb; sb.parent = sb;
  W.fake = fake;
  W.ctx = vm.createContext(sb);
  W.reset = () => {
    W.mut.length = 0; W.fetches.length = 0; W.confirms.length = 0; W.alerts.length = 0; W.errors.length = 0;
    W.reloads = 0; W.disabled.clear();
  };
  W.dispose = () => { for (const h of W.timers) clearTimeout(h); W.timers.clear(); W.timerBudget = 0; };
  W.boot = async () => {
    W.src = pageScript();
    try { vm.runInContext(W.src, W.ctx, { filename: "moments.html#script" }); } catch (e: any) { W.initErrors.push("顶层脚本出错：" + (e && e.message)); }
    for (const l of W.listeners.filter((x: any) => /^(DOMContentLoaded|load|pageshow)$/.test(x.type))) {
      try { l.fn.call(l.el === "window" ? sb : l.el, { type: l.type, target: doc }); } catch (e: any) { W.initErrors.push("就绪处理出错：" + (e && e.message)); }
    }
    await settle();
    W.initErrors.push(...W.errors);
  };
  return W;
}

function candidates(W: any): any[] {
  const out: any[] = W.listeners.filter((l: any) => l.type === "click").map((l: any) => ({ kind: "listener", name: l.name, el: l.el, fn: l.fn }));
  for (const m of W.mut) if (m[0] === "set" && m[2] === "onclick" && typeof m[3] === "function") out.push({ kind: "onclick", name: m[1], el: null, fn: m[3] });
  const names = new Set([...W.src.matchAll(/(?:function\s+|(?:const|let|var)\s+)([A-Za-z_$][\w$]*)/g)].map((m: any) => m[1]));
  for (const n of names) {
    let f: any = null;
    try { f = vm.runInContext("typeof " + n + " === 'function' ? " + n + " : null", W.ctx); } catch { /* 块内声明，取不到 */ }
    if (f && /confirm\s*\(/.test(String(f))) out.push({ kind: "global", name: n, el: null, fn: f });
  }
  return out;
}

async function fire(W: any, cand: any, only: any, except?: Set<string>) {
  const target = W.fake("点击目标", {}, MID);
  W.policy = { target, only, except: except || new Set() };
  const before = W.listeners.length;
  const ev: any = {
    type: "click", target, currentTarget: cand.el && cand.el !== "window" ? cand.el : target, srcElement: target, button: 0,
    defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {},
    stopImmediatePropagation() {}, composedPath: () => [target],
  };
  try {
    const r = cand.kind === "global" ? cand.fn.call(target, target, target) : cand.fn.call(ev.currentTarget, ev);
    if (r && typeof r.then === "function") await r.catch((e: any) => W.errors.push("处理函数出错：" + (e && e.message)));
  } catch (e: any) { W.errors.push("处理函数出错：" + (e && e.message)); }
  await settle();
  for (const l of W.listeners.slice(before)) { // 点击后才挂上的动画结束处理（过渡完再移除卡片一类）
    if (/^(transitionend|animationend)$/.test(l.type)) { try { l.fn.call(l.el, { type: l.type, target: l.el }); } catch { /* 留给断言 */ } }
  }
  await settle();
  W.policy = null;
  return target;
}

let MEMO: any; // 三条用例找的是同一个处理：第一次找到后记下序号与触发方式；找不到记 false，后面的用例不再重找

async function tryFire(W: any, c: any, only: any, except?: Set<string>) {
  W.reset();
  W.confirmAnswer = false;
  W.respond = () => ({ status: 200, body: {} });
  await fire(W, c, only, except);
  if (DEBUG) {
    console.error("[试触发]", c.kind, c.name, only, [...(except || [])].join(","), "确认:", JSON.stringify(W.confirms),
      "改动:", W.mut.length, "请求:", W.fetches.map((f: any) => (f.init.method || "GET") + " " + f.url).join(", "), W.errors.join("; "));
  }
  return W.confirms.includes(DEL_TEXT);
}

async function findDelete(W: any) {
  const cands = candidates(W);
  const hit = (i: number, only: any, except: Set<string>) => { MEMO = { index: i, only, except }; return { cand: cands[i], only, except, count: cands.length }; };
  if (MEMO === false) return { cand: null, count: cands.length, only: null, except: new Set<string>() };
  if (MEMO && MEMO.index < cands.length && await tryFire(W, cands[MEMO.index], MEMO.only, MEMO.except)) return hit(MEMO.index, MEMO.only, MEMO.except);
  const seen: any[] = [];
  // 第一轮：点击目标只匹配某一个选择器（事件委托按选择器分支；直接绑在按钮上的处理任何方式都能触发）
  for (let i = 0; i < cands.length; i++) {
    W.sels = new Set();
    W.dsels = new Set();
    const tried: any[] = [];
    const queue: any[] = [null];
    while (queue.length && tried.length < 30) {
      const only = queue.shift();
      tried.push(only);
      if (await tryFire(W, cands[i], only, new Set())) return hit(i, only, new Set());
      for (const s of W.sels) if (!tried.includes(s) && !queue.includes(s)) queue.push(s);
      if (!queue.length && !tried.includes("*")) queue.push("*");
    }
    seen.push({ tried: tried.filter((o) => o !== null), keys: [...W.dsels] });
  }
  // 第二轮：再排除一个选择器或数据键（同一类按钮再按所在位置或数据属性分评论与动态）
  for (let i = 0; i < cands.length; i++) {
    let budget = 80;
    for (const only of seen[i].tried) {
      for (const x of seen[i].keys) {
        if (budget-- <= 0) break;
        if (await tryFire(W, cands[i], only, new Set([x]))) return hit(i, only, new Set([x]));
      }
    }
  }
  MEMO = false;
  return { cand: null, count: cands.length, only: null, except: new Set<string>() };
}

const mkey = (m: any[]) => m.map(String).join("|");

async function setup() {
  const W = makeWorld();
  await W.boot();
  if (DEBUG) console.error("[脚本就绪] 点击处理", W.listeners.filter((l: any) => l.type === "click").length, "个；报错：", W.initErrors);
  const found: any = await findDelete(W);
  assert.ok(found.cand, "页面上找不到删除动态的点击处理：试触发了 " + found.count + " 个点击处理，没有一个弹出确认「" + DEL_TEXT + "」"
    + (W.initErrors.length ? "；脚本执行时报错：" + W.initErrors.slice(0, 3).join(" | ") : ""));
  // 事件委托时，同一个处理在"点到别处"时也可能改页面（如收起菜单）；这部分改动记为基线，断言时扣掉
  let baseline = new Set<string>();
  if (found.only !== null || found.except.size) {
    W.reset();
    W.confirmAnswer = false;
    W.respond = () => ({ status: 200, body: {} });
    await fire(W, found.cand, null, new Set());
    baseline = new Set(W.mut.map(mkey));
  }
  return {
    W,
    fresh: () => W.mut.filter((m: any[]) => !baseline.has(mkey(m))),
    click: async (answer: boolean, respond: any) => {
      W.reset();
      W.confirmAnswer = answer;
      W.respond = respond;
      return fire(W, found.cand, found.only, found.except);
    },
  };
}

const isDelete = (f: any) => String(f.init.method || "GET").toUpperCase() === "DELETE";
const headerOf = (h: any, name: string) => {
  if (!h) return undefined;
  if (typeof h.get === "function") return h.get(name);
  const k = Object.keys(h).find((x) => x.toLowerCase() === name);
  return k === undefined ? undefined : h[k];
};
const removed = (W: any, muts: any[]) => W.reloads > 0 || muts.some((m) => ["remove", "removeChild", "replaceWith"].includes(m[0])
  || (m[0] === "set" && m[2] === "hidden" && m[3] === true) || (m[0] === "set" && m[2] === "outerHTML")
  || (m[0] === "style" && m[2] === "display" && m[3] === "none") || (m[0] === "attr" && m[2] === "hidden"));
const shown = (W: any, msg: string) => W.alerts.some((a: string) => a.includes(msg))
  || W.mut.some((m: any[]) => m[0] === "set" && ["textContent", "innerText", "innerHTML"].includes(m[2]) && String(m[3]).includes(msg));
const answerDelete = (status: number, body: any) => (_url: string, init: any) =>
  (String(init.method || "GET").toUpperCase() === "DELETE" ? { status, body } : { status: 200, body: {} });

test("点删除先弹确认、文案逐字一致；点取消不发请求、页面不变", async () => {
  const { W, click, fresh } = await setup();
  try {
    await click(false, answerDelete(200, { ok: true, id: 42 }));
    assert.deepEqual(W.confirms, [DEL_TEXT], "应当只弹一次确认，文案逐字一致");
    assert.deepEqual(W.fetches.map((f: any) => (f.init.method || "GET") + " " + f.url), [], "点取消后不应发出任何请求");
    assert.deepEqual(fresh(), [], "点取消后页面不应有任何改动");
    assert.equal(W.reloads, 0, "点取消后不应刷新页面");
  } finally { W.dispose(); }
}, 60000);

test("确认后发 DELETE /api/moment/<编号>（JSON 头、请求体 {}）；成功后卡片从页面去掉", async () => {
  const { W, click, fresh } = await setup();
  try {
    await click(true, answerDelete(200, { ok: true, id: 42 }));
    const dels = W.fetches.filter(isDelete);
    assert.equal(dels.length, 1, "确认后应恰好发一次 DELETE，实际请求：" + W.fetches.map((f: any) => (f.init.method || "GET") + " " + f.url).join(", "));
    assert.match(dels[0].url, /(^|[^\w])\/api\/moment\/42$/, "地址应为 /api/moment/42");
    assert.match(String(headerOf(dels[0].init.headers, "content-type")), /^application\/json\b/i, "Content-Type 应为 application/json");
    assert.equal(dels[0].init.body, "{}", "请求体应为 {}");
    assert.ok(removed(W, fresh()), "成功后这张卡片应从页面去掉（没看到移除、隐藏或刷新）");
    assert.deepEqual(W.alerts, [], "成功时不应弹错误");
  } finally { W.dispose(); }
}, 60000);

test("失败时弹出「删除失败：<返回的 error>」，卡片保留，按钮恢复可点", async () => {
  const { W, click, fresh } = await setup();
  try {
    await click(true, answerDelete(403, { error: "只能删自己发的动态" }));
    assert.equal(W.fetches.filter(isDelete).length, 1, "确认后应发出 DELETE");
    assert.ok(shown(W, "删除失败：只能删自己发的动态"), "应弹出「删除失败：只能删自己发的动态」，实际弹窗：" + JSON.stringify(W.alerts));
    assert.equal(removed(W, fresh()), false, "失败时卡片应保留");
    assert.deepEqual([...W.disabled], [], "失败后按钮应恢复可点（仍有被禁用的元素）");
  } finally { W.dispose(); }
}, 60000);
