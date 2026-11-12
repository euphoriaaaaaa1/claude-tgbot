// 表情包页模板的静态断言（读 moments/templates/stickers.html 的源码文本）。
// 依据：不加载站外资源、接口与用户的文字只用安全方式写入、判断逻辑只在 StickersUX、两个标签页与搜索、
// 卡片（懒加载缩略图、改标签回车保存 Esc 取消、移除先确认）、nsfw 默认模糊且开关不记住、添加区三个入口与待加清单、
// 一行一行地传、离开提醒、手机宽度。
// 公开版差异：页面自己的吸顶顶栏类名是 .stk-top（共用片段 _console_nav.html 自带 .topbar，两者不能同名）；
// 页面 include _console_nav.html，这是模板里唯一允许的模板语句。
// 模板读不到时每个用例各自失败，不会整文件变成 0 个用例。
import { test, expect } from "bun:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const TEMPLATE = process.env.SUX_STICKERS_TEMPLATE
  || join(resolve(import.meta.dir, "..", "..", ".."), "moments", "templates", "stickers.html");
let cached: string | null = null;
const html = (): string => {
  if (cached === null) cached = readFileSync(TEMPLATE, "utf8");
  return cached;
};
const INCLUDE_RE = /\{%-?\s*include\s+(['"])_console_nav\.html\1\s*-?%\}/g;

const TAG_RE = /<[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?\/?>/g;
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const hasAttr = (tag: string, name: string, value: string) =>
  new RegExp("(?<![\\w-])" + esc(name) + "\\s*=\\s*(['\"])" + esc(value) + "\\1").test(tag);
const hasBare = (tag: string, name: string) => new RegExp("\\s" + esc(name) + "(?=[\\s=>/])").test(tag);
const tagName = (tag: string) => tag.match(/^<([a-zA-Z][a-zA-Z0-9-]*)/)![1].toLowerCase();
const tags = (text: string, name: string, value: string) => (text.match(TAG_RE) || []).filter((t) => hasAttr(t, name, value));
const one = (text: string, name: string, value: string) => {
  const found = tags(text, name, value);
  assert.equal(found.length, 1, "含 " + name + "=" + value + " 的开标签应恰好一处，实际 " + found.length);
  return found[0];
};
const textAfter = (text: string, tag: string) => text.slice(text.indexOf(tag) + tag.length).match(/[^<]*/)![0].trim();
const uxBlock = () => {
  const b = [...html().matchAll(/<script\s+id=(['"])uxLogic\1\s*>([\s\S]*?)<\/script>/g)];
  assert.equal(b.length, 1);
  return b[0][2];
};
const mainScript = () => {
  const b = [...html().matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.equal(b.length, 1, "不带属性的主脚本块应恰好一个");
  return b[0][1];
};
const fnBody = (name: string) => {
  const m = mainScript().match(new RegExp("(?:async )?function " + name + "\\b[\\s\\S]*?\\n}"));
  assert.ok(m, "主脚本里没有函数 " + name);
  return m![0];
};
const css = () => [...html().matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n")
  .replace(/\/\*[\s\S]*?\*\//g, "");
const flatCss = () => css().replace(/\s+/g, "");
const phoneCss = () => {
  const c = css();
  let out = "";
  for (const m of c.matchAll(/@media\s*\(\s*max-width\s*:\s*430px\s*\)\s*\{/g)) {
    let depth = 1, k = m.index! + m[0].length;
    while (k < c.length && depth) { depth += ({ "{": 1, "}": -1 } as Record<string, number>)[c[k]] || 0; k += 1; }
    out += c.slice(m.index! + m[0].length, k - 1);
  }
  return out.replace(/\s+/g, "");
};
const tplInner = (id: string) => html().match(new RegExp('<template id="' + id + '">([\\s\\S]*?)</template>'))![1];

// ─────────────────────────────── 公开版：共用顶栏与页面自己的顶栏
test("页面 include 共用顶栏 _console_nav.html 恰好一次", () => {
  expect([...html().matchAll(INCLUDE_RE)].length).toBe(1);
});

test("页面自己的顶栏不叫 topbar：标记里没有 class=\"topbar\"，样式里没有 .topbar 选择器", () => {
  assert.ok(!/class\s*=\s*(['"])([^'"]*\s)?topbar(\s[^'"]*)?\1/.test(html()), "页面自己用了 topbar 类名");
  assert.ok(!/\.topbar\b/.test(css()), "页面样式里定义了 .topbar");
});

test("注释与正文不再提 life.html", () => {
  assert.ok(!html().includes("life.html"));
});

// ─────────────────────────────── 不得出现的东西
test("不加载任何站外资源：没有 link、没有带 src 的资源标签、没有地址字面量与 @import / url(", () => {
  const h = html();
  for (const lit of ["fonts.googleapis.com", "fonts.gstatic.com", "@import", "url(", "http://", "https://"]) {
    assert.ok(!h.includes(lit), "出现了 " + lit);
  }
  assert.ok(!/<link\b/i.test(h), "有 <link");
  const bad = (h.match(TAG_RE) || []).filter((t) =>
    ["script", "img", "iframe", "video", "audio", "source", "embed", "object"].includes(tagName(t)) && hasBare(t, "src"));
  assert.deepEqual(bad, []);
});

test("不把任何字符串当标记解析，不在浏览器里存东西（nsfw 开关也不记住）", () => {
  const h = html();
  for (const lit of ["innerHTML", "insertAdjacentHTML", "document.write", "outerHTML", "innerText", "DOMParser",
    "createContextualFragment", "localStorage", "sessionStorage", "indexedDB", "document.cookie", "eval(", "new Function"]) {
    assert.ok(!h.includes(lit), "出现了 " + lit);
  }
});

test("字体栈只用系统字体回退；除 include 共用顶栏外不用任何模板变量、模板语句或模板注释", () => {
  const h = html();
  const body = h.match(/--font-body\s*:\s*([^;}]+)/);
  assert.ok(body && body[1].includes("-apple-system") && body[1].includes("PingFang SC") && body[1].trim().endsWith("sans-serif"));
  const mono = h.match(/--font-mono\s*:\s*([^;}]+)/);
  assert.ok(mono && mono[1].includes("ui-monospace") && mono[1].includes("Menlo") && mono[1].trim().endsWith("monospace"));
  const rest = h.replace(INCLUDE_RE, "");
  for (const s of ["{{", "{%", "{#"]) assert.ok(!rest.includes(s), "出现了 " + s);
});

test("不写死本机路径或表情库的目录结构", () => {
  for (const s of ["/Users/", "~/", ".claude", "resource/stickers", "stickers.json", "_shared", ".removed"]) {
    assert.ok(!html().includes(s), "出现了 " + s);
  }
});

// ─────────────────────────────── 顶部
test("顶部（.stk-top）：回总览的链接、标题、两档条数、刷新；提示条在顶栏里、悬浮在顶栏正下方", () => {
  const m0 = html().match(/<div class="stk-top">([\s\S]*?)<\/div>\s*<\/div>/);
  assert.ok(m0, "没有 <div class=\"stk-top\">");
  const top = m0![1];
  assert.ok(/<h1>表情包<\/h1>/.test(top));
  const back = one(top, "href", "/console");
  assert.ok(tagName(back) === "a" && textAfter(top, back) === "总览");
  one(top, "id", "countsLine");
  assert.equal(textAfter(top, one(top, "id", "reloadBtn")), "刷新");
  const op = one(top, "id", "opMsg");
  assert.ok(hasAttr(op, "role", "status") && hasBare(op, "hidden"));
  assert.ok(top.indexOf(op) > top.indexOf(one(top, "id", "reloadBtn")), "提示条排在顶栏按钮之后");
  assert.ok(hasAttr(one(html(), "id", "opMsgClose"), "aria-label", "关闭这条提示"));
  const c = flatCss();
  assert.ok(/\.stk-top\{[^}]*position:sticky;top:0;/.test(c), "顶栏吸顶");
  const m = c.match(/\.stk-top\.banner\.notice\{([^}]*)\}/);
  assert.ok(m && m[1].includes("position:absolute") && m[1].includes("top:calc(100%+8px)") && m[1].includes("margin:0"));
  assert.ok(m![1].includes("var(--tint)") && m![1].includes("var(--card)"));
  assert.ok(/\.stk-top\.banner\.notice\{left:14px;right:14px;?\}/.test(phoneCss()));
  const main = mainScript();
  const set = fnBody("setOpMsg");
  assert.ok(set.includes("document.getElementById('opMsgText').textContent = text") && set.includes("m.hidden = !text"));
  assert.ok(main.includes("document.getElementById('opMsgClose').addEventListener('click', () => setOpMsg('', 'ok'))"));
  assert.ok(main.includes("$('countsLine').textContent = has ? StickersUX.countsText(VIEW) : ''"));
});

test("视口 meta 与页面级元素恰一个", () => {
  const vp = one(html(), "name", "viewport");
  assert.ok(hasAttr(vp, "content", "width=device-width, initial-scale=1, viewport-fit=cover"));
  for (const id of ["opMsg", "opMsgText", "opMsgClose", "banners", "loadingHint", "addPanel", "libPanel", "searchInput",
    "grid-sfw", "grid-nsfw", "emptyHint", "incompleteHint", "pendingBox", "pendingRows", "addAllBtn", "labelHint",
    "categoryList"]) one(html(), "id", id);
  assert.ok(hasBare(one(html(), "id", "addPanel"), "hidden") && hasBare(one(html(), "id", "libPanel"), "hidden"), "读到数据前两块都收起");
  assert.ok(!/<dialog\b/.test(html()), "移除用浏览器自带的确认框，本页没有对话框");
});

// ─────────────────────────────── 标签页、搜索、卡片
test("两个标签页「普通（sfw）」「成人（nsfw）」、搜索框按标签和分类过滤", () => {
  const h = html();
  const t = tags(h, "role", "tab");
  assert.equal(t.length, 2);
  assert.ok(hasAttr(t[0], "data-group", "sfw") && hasAttr(t[0], "aria-selected", "true") && hasAttr(t[0], "aria-controls", "grid-sfw"));
  assert.ok(hasAttr(t[1], "data-group", "nsfw") && hasAttr(t[1], "aria-selected", "false") && hasAttr(t[1], "aria-controls", "grid-nsfw"));
  assert.equal(textAfter(h, t[0]), "普通（sfw）");
  assert.equal(textAfter(h, t[1]), "成人（nsfw）");
  for (const x of t) assert.ok(tagName(x) === "button" && hasAttr(x, "type", "button"));
  assert.equal(tags(h, "role", "tabpanel").length, 2);
  assert.ok(hasBare(one(h, "id", "grid-nsfw"), "hidden"), "初始显示普通档");
  assert.ok(hasAttr(one(h, "id", "searchInput"), "type", "search"));
  const filter = fnBody("applyFilter");
  assert.ok(filter.includes("StickersUX.filterItems(all, q)") && filter.includes("n.hidden = !keep.has(it)"), "只隐藏不匹配的卡片，不重画");
  assert.ok(filter.includes("StickersUX.tabCount(") && filter.includes("StickersUX.emptyText("));
  assert.ok(mainScript().includes("$('searchInput').addEventListener('input', applyFilter)"));
});

test("卡片：缩略图懒加载、标签、分类、大小；改标签与移除两个按钮；图片不在了有说明", () => {
  const card = tplInner("cardTpl");
  const img = one(card, "data-role", "thumb-img");
  assert.ok(tagName(img) === "img" && hasAttr(img, "loading", "lazy") && !hasBare(img, "src"), "loading=\"lazy\" 写在模板里，src 运行期再给");
  for (const r of ["card", "thumb", "thumb-note", "label", "category", "size", "edit", "edit-label", "edit-category", "edit-err",
    "edit-save", "edit-cancel", "actions"]) one(card, "data-role", r);
  assert.ok(hasBare(one(card, "data-role", "edit"), "hidden") && hasBare(one(card, "data-role", "category"), "hidden"));
  assert.ok(hasAttr(one(card, "data-role", "edit-category"), "list", "categoryList"), "改分类时可从现有分类里选");
  const acts = tags(card, "data-role", "card-action");
  assert.deepEqual(acts.map((x) => textAfter(card, x)), ["改标签", "移除"]);
  assert.ok(hasAttr(acts[0], "data-action", "edit") && hasAttr(acts[1], "data-action", "remove") && /btn-danger/.test(acts[1]));
  const render = fnBody("renderCard");
  for (const s of ["node.querySelector('[data-role=\"label\"]').textContent = it.label", "cat.textContent = it.category",
    "StickersUX.sizeText(it.size)", "note.textContent = StickersUX.MISSING_TEXT", "img.src = StickersUX.thumbUrl(it)", "if (it.missing)"]) {
    assert.ok(render.includes(s), "缺少：" + s);
  }
  assert.ok(render.indexOf("addEventListener('load'") < render.indexOf("img.src ="), "先挂监听再给地址");
});

test("改标签：原地编辑，回车保存、Esc 取消（输入法选字时不算），只发改了的键，失败留在编辑态", () => {
  const main = mainScript();
  const key = main.match(/grid\.addEventListener\('keydown', \(e\) => \{[\s\S]*?\n {2}\}\);/)![0];
  assert.ok(key.includes("e.isComposing") && key.includes("e.keyCode === 229"), "中文输入法按回车选字时不保存");
  assert.ok(key.includes("e.key === 'Enter'") && key.includes("saveEdit(node)") && key.includes("e.key === 'Escape'") && key.includes("closeEdit()"));
  const save = fnBody("saveEdit");
  assert.ok(save.includes("StickersUX.buildPatchBody(it, {") && save.includes("if (built.unchanged) closeEdit();"));
  assert.ok(save.includes("patchJson(StickersUX.itemApiUrl(it), built.body)") && save.includes("StickersUX.patchResult(r)"));
  assert.ok(save.includes("if (res.kind === 'err') { setEditErr(node, res.text); return; }") && save.includes("await load()"));
  const open = fnBody("openEdit");
  assert.ok(open.includes("lab.value = it.label") && open.includes("cat.hidden = it.group !== 'nsfw'") && open.includes("lab.focus()"));
  assert.ok(fnBody("setEditErr").includes("e.textContent = text"));
});

test("移除：先弹确认框（写出这张的标签），确认后才发 DELETE；结果写在提示条，然后重新读列表", () => {
  const rm = fnBody("removeItem");
  assert.ok(rm.indexOf("if (!confirm(StickersUX.removeConfirmText(it))) return;") >= 0);
  assert.ok(rm.indexOf("confirm(") < rm.indexOf("deleteJson("), "先确认再发请求");
  assert.ok(rm.includes("deleteJson(StickersUX.itemApiUrl(it))") && rm.includes("StickersUX.removeResult(r, it)"));
  assert.ok(rm.includes("setOpMsg(res.text, res.kind)") && rm.includes("await load()"));
  assert.ok(mainScript().includes("function deleteJson(url) { return sendJson(url, {method: 'DELETE'}); }"));
});

test("nsfw 缩略图默认模糊；「显示缩略图」开关初始关、状态不记住", () => {
  const h = html();
  assert.ok(hasAttr(one(h, "id", "grid-nsfw"), "data-blur", "1"));
  assert.ok(hasAttr(one(h, "id", "grid-sfw"), "data-blur", "0"));
  const sw = one(h, "id", "showThumbs");
  assert.ok(hasAttr(sw, "type", "checkbox") && !hasBare(sw, "checked"));
  assert.equal(textAfter(h, sw), "显示缩略图");
  assert.ok(/\.grid\[data-blur="1"\]\.thumbimg\{filter:blur\(\d+px\);?\}/.test(flatCss()));
  const main = mainScript();
  assert.ok(main.includes("$('showThumbs').addEventListener('change', (e) => setBlur(!e.target.checked))"));
  assert.ok(main.includes("setBlur(true);\nsetTab('sfw');\nload();"), "每次打开页面都从模糊开始");
  assert.ok(main.includes("window.addEventListener('pageshow', (e) => { if (e.persisted) setBlur(true); })"), "从往返缓存恢复时也回到模糊");
  assert.ok(fnBody("setTab").includes("$('nsfwTools').hidden = g !== 'nsfw'"), "开关只在成人档显示");
});

// ─────────────────────────────── 添加区
test("添加区：目标档、成人档的分类（现有分类 + 新分类…）、三个入口与提示语", () => {
  const add = html().match(/<section class="panel" id="addPanel" hidden>([\s\S]*?)<\/section>/)![1];
  const sel = one(add, "id", "targetGroup");
  assert.equal(tagName(sel), "select");
  assert.ok(add.includes('<option value="sfw">普通（sfw）</option><option value="nsfw">成人（nsfw）</option>'));
  assert.ok(hasBare(one(add, "id", "categoryField"), "hidden") && hasBare(one(add, "id", "newCategoryField"), "hidden"));
  assert.equal(tagName(one(add, "id", "targetCategory")), "select");
  for (const [id, label] of [["pickFilesBtn", "选图片"], ["pickFolderBtn", "选文件夹"], ["linksBtn", "贴链接"]]) {
    const b = one(add, "id", id);
    assert.ok(tagName(b) === "button" && hasAttr(b, "type", "button"));
    assert.equal(textAfter(add, b), label);
  }
  const files = one(add, "id", "filesInput");
  assert.ok(hasAttr(files, "type", "file") && hasBare(files, "multiple"));
  assert.ok(hasAttr(files, "accept", "image/gif,image/jpeg,image/png,image/webp,.gif,.jpg,.jpeg,.png,.webp"), "accept 只限四种");
  const folder = one(add, "id", "folderInput");
  assert.ok(hasAttr(folder, "type", "file") && hasBare(folder, "webkitdirectory"));
  assert.ok(!/\.picker\{[^}]*display:none/.test(flatCss()), "选择框不用 display:none 藏，老 Safari 才能代点");
  assert.equal(tagName(one(add, "id", "linksInput")), "textarea");
  const main = mainScript();
  const fill = fnBody("fillCategories");
  assert.ok(fill.includes("StickersUX.categoryOptions(VIEW.categories)") && fill.includes("opt.setAttribute('data-new', '1')") && fill.includes("opt.textContent = o.label"));
  assert.ok(main.includes("StickersUX.pickedCategory(isNewCategory()"));
  assert.ok(main.includes("$('labelHint').textContent = StickersUX.HINT_TEXT"));
  assert.ok(main.includes("if (!FOLDER_OK) { setOpMsg(StickersUX.FOLDER_TEXT, 'warn'); return; }") && main.includes("StickersUX.folderHint(FOLDER_OK, COARSE)"));
  assert.ok(main.includes("const FOLDER_OK = 'webkitdirectory' in document.createElement('input')"));
});

test("待加清单：预览（本地文件用对象地址，链接不预览）、标签框（默认值来自 StickersUX）、来源名、去掉这一行", () => {
  const row = tplInner("rowTpl");
  for (const r of ["row", "row-thumb", "row-label", "row-state", "row-src", "row-msg"]) one(row, "data-role", r);
  const del = one(row, "data-role", "row-del");
  assert.ok(tagName(del) === "button" && hasAttr(del, "type", "button") && hasAttr(del, "aria-label", "去掉这一行"));
  const build = fnBody("buildRow");
  assert.ok(build.includes("row.preview = URL.createObjectURL(row.file)") && build.includes("thumb.textContent = '链接'"));
  assert.ok(build.includes("node.querySelector('[data-role=\"row-src\"]').textContent = row.src") && build.includes(".value = row.label"));
  assert.ok(fnBody("dropRow").includes("URL.revokeObjectURL(row.preview)"), "去掉一行时收回对象地址");
  const files = fnBody("addFiles");
  for (const s of ["StickersUX.isAcceptedFile(f.name, f.type)", "StickersUX.isHeic(f.name, f.type)", "StickersUX.defaultLabelFromName(f.name)",
    "StickersUX.isTooLarge(f.size, max)", "row.state = 'fail'; row.msg = StickersUX.tooLargeText(max)", "StickersUX.pickSummary(c, max)",
    "f.webkitRelativePath || f.name"]) {
    assert.ok(files.includes(s), "缺少：" + s);
  }
  const links = fnBody("addLinks");
  assert.ok(links.includes("StickersUX.parseLinks($('linksInput').value)") && links.includes("StickersUX.defaultLabelFromUrl(url)"));
  assert.ok(links.includes("$('linksInput').value = parsed.bad.join('\\n')") && links.includes("StickersUX.badLinksText(parsed.bad)"));
  const paint = fnBody("paintRow");
  assert.ok(paint.includes("StickersUX.rowBadge(row.state)") && paint.includes("tag.textContent = badge.text") && paint.includes("msg.textContent = row.msg"));
});

test("全部加入：先核对目标档；一行一行地传（本地文件用 FileReader 转 base64）；某行失败不停；做完给汇总并重新读列表；进行中禁用", () => {
  const all = fnBody("addAll");
  assert.ok(all.includes("StickersUX.checkTarget($('targetGroup').value, currentCategory())"));
  assert.ok(all.includes("ROWS.filter((r) => StickersUX.shouldSend(r.state))"));
  assert.ok(/for \(const row of todo\) \{[\s\S]*?const res = await sendRow\(row, t\);/.test(all), "逐行 await，不并发");
  assert.ok(!all.includes("Promise.all"));
  assert.ok(all.indexOf("UPLOADING = true") < all.indexOf("await sendRow") && /finally \{\s*UPLOADING = false;/.test(all));
  assert.ok(all.includes("StickersUX.progressText(") && all.includes("StickersUX.summarize(todo.map((r) => r.state))"));
  assert.ok(all.indexOf("setOpMsg(sum.text, sum.kind)") < all.indexOf("await load()"));
  const send = fnBody("sendRow");
  assert.ok(send.includes("postJson(API + '/link', built.body)") && send.includes("postJson(API + '/upload', built.body)"));
  assert.ok(send.includes("StickersUX.buildLinkBody(") && send.includes("StickersUX.buildUploadBody(") && send.includes("StickersUX.addResult("));
  assert.ok(send.indexOf("StickersUX.validateLabel(row.label)") < send.indexOf("readAsBase64(row.file)"), "标签不对就不读文件");
  const read = fnBody("readAsBase64");
  assert.ok(read.includes("new FileReader()") && read.includes("fr.readAsDataURL(file)") && read.includes("StickersUX.base64FromDataUrl(fr.result)"));
  const ctl = fnBody("updateControls");
  assert.ok(ctl.includes("$('addAllBtn').disabled = lock ||") && ctl.includes("b.disabled = lock"));
  assert.ok(ctl.includes("'pickFilesBtn', 'pickFolderBtn', 'linksBtn'") && ctl.includes("$(id).disabled = UPLOADING"));
});

test("离开提醒：清单里还有没加成的行、或正在上传时，关页 / 跳走由浏览器弹原生提醒", () => {
  const unload = mainScript().match(/window\.addEventListener\('beforeunload', \(e\) => \{[\s\S]*?\n\}\);/)![0];
  assert.ok(unload.includes("StickersUX.needsLeaveWarning(ROWS.map((r) => r.state), UPLOADING)"));
  assert.ok(unload.includes("e.preventDefault();") && unload.includes("e.returnValue = ''"));
});

// ─────────────────────────────── 纯函数块与主脚本的分工、接口
const API = ["listView", "loadPlan", "countsText", "tabCount", "incompleteText", "emptyText", "sizeText", "thumbUrl", "itemApiUrl",
  "filterItems", "removeConfirmText", "checkTarget", "categoryOptions", "pickedCategory", "buildPatchBody", "defaultLabelFromName",
  "defaultLabelFromUrl", "isAcceptedFile", "isHeic", "isTooLarge", "tooLargeText", "parseLinks", "badLinksText", "fileKey", "linkKey",
  "pickSummary", "folderHint", "base64FromDataUrl", "validateLabel", "buildUploadBody", "buildLinkBody", "addResult", "rowBadge",
  "isDone", "shouldSend", "needsLeaveWarning", "progressText", "summarize", "patchResult", "removeResult"];

test("主脚本经 StickersUX 调用判断逻辑，不另写一份同名函数", () => {
  const main = mainScript();
  for (const fn of API) {
    assert.ok(main.includes("StickersUX." + fn + "("), "主脚本没有调用 StickersUX." + fn);
    assert.ok(!new RegExp("(function\\s+|(const|let|var)\\s+)" + fn + "\\b").test(main), "主脚本另写了 " + fn);
  }
  assert.ok(!/StickersUX\.[A-Za-z_]\w*\(/.test(uxBlock()), "纯函数块内部不经导出对象自调");
});

test("接口：列表 GET、加图与贴链接是 JSON 的 POST、改标签是 JSON 的 PATCH、移除是 DELETE（apiFetch 补 {} 体）；401 跳登录页", () => {
  const main = mainScript();
  assert.ok(main.includes("const API = StickersUX.API_BASE;") && main.includes("r = await jsonFetch(API);"));
  assert.ok(main.includes("{method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}"));
  assert.ok(main.includes("{method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}"));
  const api = fnBody("apiFetch");
  assert.ok(api.includes("method !== 'GET' && method !== 'HEAD' && opts.body == null") && api.includes("body: '{}'"));
  assert.ok(api.includes("location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search)"));
  assert.equal((main.match(/_origFetch\(/g) || []).length, 1);
  assert.ok(!/['"]\/api\//.test(main), "主脚本不自己写接口地址，一律从 StickersUX 取");
  const plan = fnBody("load");
  assert.ok(plan.includes("StickersUX.loadPlan(r)") && plan.includes("if (plan.broken) { BROKEN = true; VIEW = null; }") && plan.includes("showLoadError(plan.text)"));
  assert.ok(fnBody("updateControls").includes("const lock = OP_BUSY || UPLOADING || BROKEN;"), "库坏了时一切写入按钮禁用");
});

test("接口与用户的文字只用 textContent / value 写入；控件用 createElement 造", () => {
  const main = mainScript();
  assert.ok(!/\.innerText\s*=/.test(main) && !/\.textContent\s*\+=/.test(main));
  for (const s of ["createElement('option')", "opt.textContent = o.label", "opt.value = o.value", "b.textContent = text", "img.alt = it.label"]) {
    assert.ok(main.includes(s), "缺少：" + s);
  }
  assert.ok(!/setAttribute\('(on\w+|style|href|src)'/.test(main), "不经 setAttribute 写事件、样式或地址");
});

// ─────────────────────────────── 手机可用在样式上的体现（真机表现仍需人工看）
test("窄屏（≤430px）：两列卡片、控件 16px 字号且不小于 44px、标签页铺满一行、搜索框独占一行；长串可断行", () => {
  const phone = phoneCss();
  assert.ok(phone, "没有 @media (max-width:430px) 块");
  assert.ok(/\.grid\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\);/.test(phone), "两列，minmax(0,…) 防长串撑宽");
  assert.ok(/select,input,textarea\{min-height:44px;font-size:16px;?\}/.test(phone));
  assert.ok(/\.btn\{min-height:44px;?\}/.test(phone) && /\.icon-btn\{width:44px;height:44px;?\}/.test(phone));
  assert.ok(/\.tab\{flex:110;min-height:44px;?\}/.test(phone), "两个标签页各占半行、不小于 44px");
  assert.ok(/\.target\{grid-template-columns:1fr;?\}/.test(phone) && /\.search\{flex-basis:100%;?\}/.test(phone));
  const c = flatCss();
  assert.ok(/input,select,textarea\{[^}]*width:100%;max-width:100%;min-width:0;?\}/.test(c), "控件占满一行、不撑破视口");
  assert.ok(/\.prow-main\{[^}]*min-width:0/.test(c) && /\.scard\{[^}]*min-width:0/.test(c), "弹性与网格子项可收窄");
  for (const sel of [".mono", ".scard-label", ".hint", ".prow-msg"]) {
    assert.ok(new RegExp(esc(sel) + "\\{[^}]*overflow-wrap:anywhere").test(c), sel + " 长串可断行");
  }
  assert.ok(/\.thumb\{[^}]*aspect-ratio:1\/1/.test(c) && /\.thumbimg\{[^}]*object-fit:contain/.test(c), "缩略图方形、完整显示");
  assert.ok(/@media\(prefers-reduced-motion:reduce\)/.test(c), "照顾减少动效的设置");
});
