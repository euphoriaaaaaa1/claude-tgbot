// 朋友圈页模板的静态断言（读 moments/templates/moments.html 的源码文本）。
// 依据接口文件 1.1 与行为清单第 9、10 条：include 共用导航 _console_nav.html；共用导航外层（.topbar）不吸顶，
// 页面自己的切换栏吸顶；共用导航的 .btn 有样式；不含生图引擎按钮与接口、不引用站外资源、不含"三个 bot"。
// 模板读不到时每个用例各自失败，不会整文件变成 0 个用例。
import { test } from "bun:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const TEMPLATE = process.env.PMOM_MOMENTS_TEMPLATE
  || join(resolve(import.meta.dir, "..", "..", ".."), "moments", "templates", "moments.html");
let cached: string | null = null;
const html = (): string => {
  if (cached === null) cached = readFileSync(TEMPLATE, "utf8");
  return cached;
};
const INCLUDE_RE = /\{%-?\s*include\s+(['"])_console_nav\.html\1[^%]*-?%\}/g;

type Rule = { selector: string; body: string };
const rules = (): Rule[] => {
  const css = [...html().matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n")
    .replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Rule[] = [];
  // 只取最内层的"选择器 { 声明 }"（@media 里的也算）
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selector: m[1].trim(), body: m[2].replace(/\s+/g, "") });
  }
  return out;
};
const selectorHas = (sel: string, cls: string) =>
  sel.split(",").some((s) => new RegExp("\\." + cls + "(?![\\w-])").test(s));

test("模板 include 共用导航 _console_nav.html 恰好一次", () => {
  assert.equal([...html().matchAll(INCLUDE_RE)].length, 1);
});

test("共用导航外层 .topbar 在本页被覆盖成不吸顶（position:static）", () => {
  const hit = rules().filter((r) => selectorHas(r.selector, "topbar") && /position:static/.test(r.body));
  assert.ok(hit.length >= 1, "没有把 .topbar 设成 position:static 的规则");
});

test("页面自己的切换栏吸顶：有不是 .topbar 的规则设 position:sticky", () => {
  const hit = rules().filter((r) => !selectorHas(r.selector, "topbar") && /position:sticky/.test(r.body));
  assert.ok(hit.length >= 1, "页面自己没有吸顶的栏");
});

test("页面自己的顶栏不叫 topbar：标记里没有 class=\"topbar\"", () => {
  assert.ok(!/class\s*=\s*(['"])([^'"]*\s)?topbar(\s[^'"]*)?\1/.test(html()), "页面自己用了 topbar 类名");
});

test("共用导航的 .btn 链接在本页有样式", () => {
  const hit = rules().filter((r) => selectorHas(r.selector, "btn") && r.body.length > 0);
  assert.ok(hit.length >= 1, "本页样式里没有 .btn 规则");
});

test("不含生图引擎按钮与接口", () => {
  for (const s of ["engine-chip", "/api/moments/image_engine", "image_provider"]) {
    assert.ok(!html().includes(s), "模板里出现了 " + s);
  }
});

test("不引用站外脚本、样式、字体、图片", () => {
  const t = html();
  const bad = [
    ...t.matchAll(/<(?:script|link|img|iframe)\b[^>]*\b(?:src|href)\s*=\s*["']?(?:https?:)?\/\/[^"'\s>]*/gi),
    ...t.matchAll(/@import[^;]*/gi),
    ...t.matchAll(/url\(\s*["']?(?:https?:)?\/\/[^)]*\)/gi),
    ...t.matchAll(/fonts\.(?:googleapis|gstatic)\.com/gi),
  ].map((m) => m[0]);
  assert.deepEqual(bad, []);
});

test("不含「三个 bot」字样", () => {
  assert.ok(!html().includes("三个 bot"));
});

test("有冲突提示的位置：data-role=\"conflict\" 与「两边都在跑」", () => {
  assert.ok(html().includes('data-role="conflict"') && html().includes("两边都在跑"));
});

test("动态卡片的删除按钮用 moment-del 类", () => {
  assert.ok(/class\s*=\s*["'][^"']*(?<![\w-])moment-del(?![\w-])/.test(html()), "没有 class 含 moment-del 的元素");
});
