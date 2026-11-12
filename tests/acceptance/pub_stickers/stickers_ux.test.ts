// 表情包页纯函数块 StickersUX 的 bun 测试。
// 做法：取 moments/templates/stickers.html 里 <script id="uxLogic"> 的原文，在没有 window / document 的环境里执行，
// 拿到 StickersUX 再断言。模板读不到或块不对时，每个用例各自失败，不会整文件变成 0 个用例。
import { test, expect } from "bun:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import vm from "node:vm";

const TEMPLATE = process.env.SUX_STICKERS_TEMPLATE
  || join(resolve(import.meta.dir, "..", "..", ".."), "moments", "templates", "stickers.html");
let blockCache: string | null = null;
let uxCache: any = null;
const BLOCK = (): string => {
  if (blockCache === null) {
    const html = readFileSync(TEMPLATE, "utf8");
    const blocks = [...html.matchAll(/<script\s+id=(['"])uxLogic\1\s*>([\s\S]*?)<\/script>/g)];
    assert.equal(blocks.length, 1, "模板里应恰有一个 <script id=\"uxLogic\"> 块");
    blockCache = blocks[0][2];
  }
  return blockCache;
};
// 在本进程同一个全局环境里执行（跨 vm 上下文的数组原型不同，严格相等比较会把结构相同的值判不等）
const U = (): any => {
  if (uxCache === null) uxCache = vm.runInThisContext(BLOCK() + "\n;StickersUX;", { filename: "stickers.html#uxLogic" });
  return uxCache;
};

test("块在没有 window 的干净上下文里也能独立执行", () => {
  expect(vm.runInNewContext(BLOCK() + "\n;typeof StickersUX;", {})).toBe("object");
});

const deepFreeze = (v: any): any => { if (v && typeof v === 'object') { Object.values(v).forEach(deepFreeze); Object.freeze(v); } return v; };
const item = (extra?: any): any => Object.assign({id: 'sfw/000_77da87dcd4.gif', group: 'sfw', label: '占位一', category: null, url: '',
  file: '000_77da87dcd4.gif', size: 10232, missing: false, img: '/console/stickers/img/sfw/000_77da87dcd4.gif'}, extra || {});
const nitem = (extra?: any): any => item(Object.assign({id: 'nsfw/001_aaaaaaaaaa.png', group: 'nsfw', label: '占位二', category: '分类甲',
  file: '001_aaaaaaaaaa.png', img: '/console/stickers/img/nsfw/001_aaaaaaaaaa.png'}, extra || {}));

// ─────────────────────────────── 导出形状与纯函数块的卫生
test('导出对象含约定的全部函数与常量', () => {
  const fns = ['defaultLabelFromName', 'defaultLabelFromUrl', 'isAcceptedFile', 'parseLinks', 'validateLabel', 'buildUploadBody',
    'buildLinkBody', 'summarize', 'filterItems',   // ↑ BRIEF 点名的九个
    'isGroup', 'groupText', 'groupShort', 'listView', 'loadPlan', 'countsText', 'tabCount', 'incompleteText', 'emptyText',
    'sizeText', 'thumbUrl', 'itemApiUrl', 'removeConfirmText', 'validateCategory', 'checkTarget', 'categoryOptions',
    'pickedCategory', 'buildPatchBody', 'isHeic', 'isTooLarge', 'tooLargeText', 'isHttpUrl', 'badLinksText', 'fileKey',
    'linkKey', 'pickSummary', 'folderHint', 'base64FromDataUrl', 'failureText', 'addResult', 'rowBadge', 'isDone',
    'shouldSend', 'needsLeaveWarning', 'progressText', 'patchResult', 'removeResult'];
  for (const f of fns) assert.equal(typeof U()[f], 'function', f);
  for (const c of ['GENERIC_FAIL_TEXT', 'NETWORK_FAIL_TEXT', 'NO_CHANGE_TEXT', 'HINT_TEXT', 'FOLDER_TEXT', 'MISSING_TEXT']) {
    assert.equal(typeof U()[c], 'string', c);
  }
  assert.deepEqual(U().GROUPS, ['sfw', 'nsfw']);
  assert.equal(U().MAX_BYTES, 10485760);
  assert.equal(U().MAX_LABEL, 60);
});

test('纯函数块不碰页面、网络、时钟、存储；window 只出现在末尾导出', () => {
  // 只查调用形式的 fetch(：错误码 fetch_failed 是数据，不是请求
  for (const w of ['document', 'fetch(', 'XMLHttpRequest', 'navigator', 'location', 'setTimeout', 'setInterval',
    'Date', 'Math.random', 'localStorage', 'sessionStorage', 'FileReader', 'createObjectURL']) {
    assert.ok(!BLOCK().includes(w), '纯函数块里出现了 ' + w);
  }
  const lines = BLOCK().trim().split('\n').map((l) => l.trim()).filter((l) => l.includes('window'));
  assert.deepEqual(lines, ["if (typeof window !== 'undefined') window.StickersUX = StickersUX;"]);
  assert.ok(BLOCK().trim().endsWith(lines[0]), '导出语句应是块的最后一句');
  const top = BLOCK().split('\n').filter((l) => /^(var|let|const|function|class)\s/.test(l));
  assert.deepEqual(top.map((l) => l.split('=')[0].trim()), ['var StickersUX']);
});

test('纯函数块语法不超过 ES2019，也不用旧版 Safari 不认的正则后行断言', () => {
  for (const [re, what] of [[/\?\.[A-Za-z_(\[]/, '可选链'], [/\?\?/, '空值合并'], [/\.replaceAll\(/, 'replaceAll'],
    [/\.at\(/, 'at()'], [/\.matchAll\(/, 'matchAll'], [/(?<![\w$])#[A-Za-z_]\w*\s*[=(;]/, '私有成员'], [/\(\?<[=!]/, '后行断言']]) {
    assert.ok(!re.test(BLOCK()), '用了 ' + what);
  }
});

test('文案与 BRIEF 一致（引号用本站的「」）', () => {
  assert.equal(U().HINT_TEXT, '标签写清这张表达什么，比如「生气 2」「害羞捂脸」，bot 靠标签挑表情。新加的表情，bot 下次读表情库时就能用上。');
  assert.equal(U().FOLDER_TEXT, '手机上请用「选图片」，可多选。');
  assert.equal(U().MISSING_TEXT, '图片文件不在了');
});

// ─────────────────────────────── 默认标签
test('defaultLabelFromName：去扩展名、分隔符换空格、合并空白', () => {
  const cases = [['生气_2.gif', '生气 2'], ['害羞捂脸.webp', '害羞捂脸'], ['生气.2.png', '生气 2'], ['happy-face.PNG', 'happy face'],
    ['a__b--c..d.jpg', 'a b c d'], ['  前后 空白  .jpeg', '前后 空白'], ['没有扩展名', '没有扩展名'], ['v1.5', 'v1 5'],
    ['文件夹/子目录/开心.gif', '开心'], ['C:\\图\\委屈.png', '委屈'], ['抱抱 (1).png', '抱抱 (1)'], ['x.gifv', 'x']];
  for (const [name, want] of cases) assert.equal(U().defaultLabelFromName(name), want, name);
});

test('defaultLabelFromName：最长 60 字（按字符数，表情符号算一个），截断后不留尾空格', () => {
  const long = '字'.repeat(70) + '.gif';
  assert.equal(U().defaultLabelFromName(long), '字'.repeat(60));
  const emoji = '😀'.repeat(61) + '.png';
  assert.equal(Array.from(U().defaultLabelFromName(emoji)).length, 60);
  assert.equal(U().defaultLabelFromName('字'.repeat(59) + '_x' + '.png'), '字'.repeat(59), '第 60 个字符是空格时截掉');
});

test('defaultLabelFromName：相机 / 截图 / 聊天软件前缀、纯数字、十六进制串、泛称都返回空串', () => {
  const names = ['IMG_20240101_123456.jpg', 'IMG-20240101-WA0001.jpg', 'IMG20240101.jpg', 'img_e1234.JPG', 'DSC_0001.JPG',
    'DSCN0001.JPG', 'DSCF1234.jpg', 'DSC01234.JPG', 'PXL_20231010_123456789.jpg', 'MVIMG_20200101_000000.jpg',
    'Screenshot_2024-01-01-10-00-00.png', 'Screenshot 2024-01-01 at 10.00.00.png', 'Screen Shot 2020-01-01 at 1.00.00 PM.png',
    'Snipaste_2024-01-01_10-00-00.png', 'mmexport1700000000000.jpg', 'wx_camera_1700000000000.jpg', '微信图片_20240101123456.jpg',
    '截屏2024-01-01 10.00.00.png', '屏幕截图 2024-01-01 100000.png', 'QQ截图20240101.png', 'QQ图片20240101.jpg',
    'photo_2024-01-01_12-00-00.jpg', 'image0.jpg', 'image (3).png',
    '1700000000000.jpg', '2024-01-01.png', '(1).png', '77da87dcd4.gif', '000_77da87dcd4.gif', 'A1B2C3.png', 'abcdef.gif',
    '3f2504e0-4f89-11d3-9a0c-0305e82c3301.png', 'GxYz123AbC.jpg',
    'image.png', 'download.gif', 'unnamed.jpg', 'giphy.gif', 'tenor.gif', '下载.gif', '图片.png', '表情.gif', 'untitled.webp',
    '.gif', '___.png', '-.jpg', '', null, undefined];
  for (const n of names) assert.equal(U().defaultLabelFromName(n), '', String(n));
});

test('defaultLabelFromName：像词的名字不误伤（只有 5 位的十六进制、全小写字母加数字）', () => {
  assert.equal(U().defaultLabelFromName('cafe1.png'), 'cafe1');
  assert.equal(U().defaultLabelFromName('dog2023.gif'), 'dog2023');
  assert.equal(U().defaultLabelFromName('imgur 收藏.gif'), 'imgur 收藏');
  assert.equal(U().defaultLabelFromName('DSC 生气.jpg'), 'DSC 生气');
});

test('defaultLabelFromUrl：取路径最后一段，百分号解码，查询串与井号不看', () => {
  const cases = [['https://example.com/a/%E7%94%9F%E6%B0%94_2.gif?x=1#y', '生气 2'], ['http://example.com/stickers/委屈/', '委屈'],
    ['https://example.com/a/happy-face.webp', 'happy face'], ['https://example.com/', ''], ['https://example.com', ''],
    ['https://example.com/media/GxYz123AbC?format=jpg&name=large', ''], ['https://example.com/a/77da87dcd4.gif', ''],
    ['https://example.com/a/%E0%A4%A.gif', '%E0%A4%A'], ['  https://example.com/x/抱抱.png  ', '抱抱'], ['不是链接', ''], [null, '']];
  for (const [u, want] of cases) assert.equal(U().defaultLabelFromUrl(u), want, String(u));
});

// ─────────────────────────────── 选文件、贴链接
test('isAcceptedFile：按 MIME 定；MIME 为空或是通用二进制时看扩展名', () => {
  const yes = [['a.gif', 'image/gif'], ['a.jpg', 'image/jpeg'], ['a.png', 'image/png'], ['a.webp', 'image/webp'], ['a.jpg', 'image/jpg'],
    ['a.HEIC', 'image/jpeg'], ['a.GIF', ''], ['a.jpeg', ''], ['a.jfif', ''], ['a.webp', 'application/octet-stream'], ['a.png', ' IMAGE/PNG ']];
  const no = [['a.heic', 'image/heic'], ['a.heic', ''], ['a.svg', 'image/svg+xml'], ['a.bmp', 'image/bmp'], ['a.txt', 'text/plain'],
    ['.DS_Store', ''], ['a.png', 'text/html'], ['a.mp4', 'video/mp4'], ['a', ''], ['', ''], [null, null]];
  for (const [n, t] of yes) assert.equal(U().isAcceptedFile(n, t), true, n + ' ' + t);
  for (const [n, t] of no) assert.equal(U().isAcceptedFile(n, t), false, n + ' ' + t);
  assert.ok(U().isHeic('a.heic', '') && U().isHeic('a.HEIF', '') && U().isHeic('x', 'image/heic') && U().isHeic('x', 'image/heif-sequence'));
  assert.ok(!U().isHeic('a.jpg', 'image/jpeg'));
});

test('parseLinks：去空行、去首尾空白、只留 http / https、去重并保持先后', () => {
  const r = U().parseLinks('  https://a.example/1.gif  \r\n\n\thttp://b.example/2.png\nftp://c.example/3.gif\n不是链接\n'
    + 'https://a.example/1.gif\r不是链接\nHTTPS://D.example/4.webp\nhttps://e.example/有 空格.png\nhttps://\n');
  assert.deepEqual(r.ok, ['https://a.example/1.gif', 'http://b.example/2.png', 'HTTPS://D.example/4.webp']);
  assert.deepEqual(r.bad, ['ftp://c.example/3.gif', '不是链接', 'https://e.example/有 空格.png', 'https://']);
  assert.deepEqual(U().parseLinks(''), {ok: [], bad: []});
  assert.deepEqual(U().parseLinks(null), {ok: [], bad: []});
  const long = 'https://a.example/' + 'x'.repeat(2048);
  assert.deepEqual(U().parseLinks(long), {ok: [], bad: [long]}, '超过 2048 字符的不收');
  assert.ok(U().isHttpUrl('https://a.example/' + 'x'.repeat(2048 - 18)));
});

test('选完 / 贴完的那句话：加了几行、跳过几个（HEIC 单独点出）、超大几个、清单里已有几个、不是链接几行', () => {
  assert.deepEqual(U().pickSummary({added: 3}, 10485760), {kind: 'ok', text: '加了 3 行到待加清单。'});
  const s = U().pickSummary({added: 2, skipped: 3, heic: 1, tooLarge: 1, already: 1}, 10485760);
  assert.equal(s.kind, 'warn');
  assert.equal(s.text, '加了 2 行到待加清单；跳过 3 个不是 GIF / JPG / PNG / WEBP 的文件（其中 1 个是 HEIC，请先转成 JPG）；'
    + '1 个超过 10.0 MB，已标为失败；1 个已在清单里，没再加。');
  assert.deepEqual(U().pickSummary({}, 0), {kind: 'warn', text: '没有选到图片。'});
  assert.equal(U().pickSummary({added: 1, bad: 2}).text, '加了 1 行到待加清单；2 行不是链接，没有加。');
  assert.equal(U().badLinksText([]), '');
  assert.equal(U().badLinksText(['甲', '乙', '丙', '丁']), '有 4 行不是 http / https 链接，没有加：甲、乙、丙 等。');
  assert.equal(U().badLinksText(['字'.repeat(45)]), '有 1 行不是 http / https 链接，没有加：' + '字'.repeat(40) + '…。');
});

test('文件大小上限、文件夹提示、data 地址取 base64、清单去重的键', () => {
  assert.equal(U().isTooLarge(10485760, 10485760), false);
  assert.equal(U().isTooLarge(10485761, 10485760), true);
  assert.equal(U().isTooLarge(10485761, undefined), true, '没给上限按 10 MiB');
  assert.equal(U().tooLargeText(10485760), '超过 10.0 MB 上限，不能加。');
  assert.equal(U().folderHint(true, false), '');
  assert.equal(U().folderHint(false, false), U().FOLDER_TEXT);
  assert.equal(U().folderHint(true, true), U().FOLDER_TEXT, '触屏设备上也提示');
  assert.equal(U().base64FromDataUrl('data:image/png;base64,iVBORw0K'), 'iVBORw0K');
  assert.equal(U().base64FromDataUrl('data:application/octet-stream;base64,AAAA'), 'AAAA');
  assert.equal(U().base64FromDataUrl('data:text/plain,hello'), '');
  assert.equal(U().base64FromDataUrl('iVBORw0K'), '');
  assert.notEqual(U().fileKey('a.png', 10, 1), U().fileKey('a.png', 11, 1));
  assert.equal(U().fileKey('a.png', 10, 1), U().fileKey('a.png', 10, 1));
  assert.equal(U().linkKey(' https://a.example/1.gif '), U().linkKey('https://a.example/1.gif'));
  assert.notEqual(U().linkKey('https://a.example/1.gif'), U().fileKey('https://a.example/1.gif', '', ''));
});

// ─────────────────────────────── 标签、分类、目标档
test('validateLabel：去首尾空白后 1 到 60 字，不许换行或控制字符', () => {
  assert.deepEqual(U().validateLabel('  生气 2  '), {ok: true, value: '生气 2'});
  assert.deepEqual(U().validateLabel('字'.repeat(60)), {ok: true, value: '字'.repeat(60)});
  assert.equal(U().validateLabel('😀'.repeat(60)).ok, true, '表情符号按一个字算');
  for (const bad of ['', '   ', null, undefined, '字'.repeat(61), '😀'.repeat(61), '一\n二', '一\r二', '一\t二', '一\u0000二',
    '一\u007f二', '一\u0085二', '一\u2028二']) {
    const r = U().validateLabel(bad);
    assert.equal(r.ok, false, JSON.stringify(bad));
    assert.ok(typeof r.error === 'string' && r.error.length > 0);
  }
  assert.equal(U().validateLabel('').error, '请写标签。');
  assert.equal(U().validateLabel('字'.repeat(61)).error, '标签最多 60 个字，现在 61 个。');
  assert.equal(U().validateLabel('一\n二').error, '标签里不能有换行或控制字符。');
  assert.equal(U().validateCategory(' ').error, '请写分类。');
});

test('checkTarget：只许 sfw / nsfw；nsfw 必须带分类；sfw 不带分类', () => {
  assert.deepEqual(U().checkTarget('sfw', '分类甲'), {ok: true, group: 'sfw', category: null});
  assert.deepEqual(U().checkTarget('nsfw', ' 分类甲 '), {ok: true, group: 'nsfw', category: '分类甲'});
  assert.deepEqual(U().checkTarget('nsfw', ''), {ok: false, error: '加到成人档要先选一个分类。'});
  assert.equal(U().checkTarget('nsfw', '甲\n乙').ok, false);
  assert.equal(U().checkTarget('other', '').ok, false);
  assert.equal(U().checkTarget('', '').error, '请选要加到哪一档。');
});

test('分类下拉：首项「请选择分类」，中间现有分类去重保序，末项「新分类…」', () => {
  const o = U().categoryOptions(['乙', '甲', '乙', '', null, '丙']);
  assert.deepEqual(o.map((x) => x.kind), ['pick', 'cat', 'cat', 'cat', 'new']);
  assert.deepEqual(o.map((x) => x.label), ['请选择分类', '乙', '甲', '丙', '新分类…']);
  assert.deepEqual(o.filter((x) => x.kind === 'cat').map((x) => x.value), ['乙', '甲', '丙']);
  assert.deepEqual(U().categoryOptions(undefined).map((x) => x.kind), ['pick', 'new']);
  assert.equal(U().pickedCategory(true, '甲', '新的'), '新的');
  assert.equal(U().pickedCategory(false, '甲', '新的'), '甲');
});

// ─────────────────────────────── 请求体
test('buildUploadBody：键序 group, label, category?, filename, data；sfw 不带分类；文件名只取末段', () => {
  const r = U().buildUploadBody({group: 'nsfw', category: ' 分类甲 ', label: ' 害羞捂脸 ', filename: 'dir/sub/害羞.png', data: 'AAAA'});
  assert.ok(r.ok);
  assert.deepEqual(Object.keys(r.body), ['group', 'label', 'category', 'filename', 'data']);
  assert.deepEqual(r.body, {group: 'nsfw', label: '害羞捂脸', category: '分类甲', filename: '害羞.png', data: 'AAAA'});
  const s = U().buildUploadBody({group: 'sfw', category: '分类甲', label: '生气', filename: 'a.gif', data: 'R0lG'});
  assert.deepEqual(Object.keys(s.body), ['group', 'label', 'filename', 'data']);
  assert.equal(U().buildUploadBody({group: 'nsfw', label: '生气', filename: 'a.gif', data: 'R0lG'}).ok, false, 'nsfw 缺分类');
  assert.equal(U().buildUploadBody({group: 'sfw', label: '', filename: 'a.gif', data: 'R0lG'}).error, '请写标签。');
  assert.equal(U().buildUploadBody({group: 'sfw', label: '生气', filename: 'a.gif', data: ''}).ok, false, '没有内容');
  assert.equal(U().buildUploadBody({group: 'x', label: '生气', filename: 'a.gif', data: 'R0lG'}).ok, false);
  assert.equal(U().buildUploadBody({group: 'sfw', label: '生气', filename: '', data: 'R0lG'}).body.filename, 'image');
});

test('buildLinkBody：键序 group, label, category?, url；链接去首尾空白且只许 http / https', () => {
  const r = U().buildLinkBody({group: 'sfw', category: '不该带', label: '生气', url: '  https://a.example/1.gif '});
  assert.deepEqual(r, {ok: true, body: {group: 'sfw', label: '生气', url: 'https://a.example/1.gif'}});
  const n = U().buildLinkBody({group: 'nsfw', category: '分类甲', label: '生气', url: 'http://a.example/1.gif'});
  assert.deepEqual(Object.keys(n.body), ['group', 'label', 'category', 'url']);
  for (const url of ['ftp://a.example/1.gif', 'a.example/1.gif', '', 'javascript:alert(1)']) {
    assert.equal(U().buildLinkBody({group: 'sfw', label: '生气', url: url}).ok, false, url);
  }
});

test('buildPatchBody：只带改了的键；nsfw 可改分类；没改动不发；原来没分类、这次也没填，不算改', () => {
  assert.deepEqual(U().buildPatchBody(item(), {label: ' 新标签 '}), {ok: true, body: {label: '新标签'}});
  assert.deepEqual(U().buildPatchBody(item(), {label: '占位一', category: '不该带'}), {ok: false, unchanged: true, error: U().NO_CHANGE_TEXT});
  assert.deepEqual(U().buildPatchBody(nitem(), {label: '占位二', category: '分类乙'}), {ok: true, body: {category: '分类乙'}});
  assert.deepEqual(U().buildPatchBody(nitem(), {label: '新', category: '分类乙'}), {ok: true, body: {label: '新', category: '分类乙'}});
  assert.deepEqual(U().buildPatchBody(nitem({category: null}), {label: '新', category: ' '}), {ok: true, body: {label: '新'}});
  assert.equal(U().buildPatchBody(nitem(), {label: '新', category: ''}).error, '请写分类。', '原来有分类的不许清空');
  assert.equal(U().buildPatchBody(item(), {label: '\n'}).ok, false);
});

// ─────────────────────────────── 列表与卡片
const LIST_BODY = {ok: true, groups: {
  sfw: [item(), 'not-an-object', null, item({id: 'sfw/002_bbbbbbbbbb.jpg', file: '002_bbbbbbbbbb.jpg', label: '生气 2', missing: true,
    size: 'x', category: '不该有'})],
  nsfw: [nitem(), nitem({id: 'nsfw/003_cccccccccc.webp', file: '003_cccccccccc.webp', label: '害羞捂脸', category: '分类乙'})]},
  categories: ['分类甲', '分类乙', '分类甲', ''], incomplete: {sfw: 1, nsfw: 0}, limits: {max_bytes: 10485760, types: ['gif', 'jpg', 'png', 'webp']}};

test('listView：不是对象的条目不画；sfw 不带分类；大小只认数字；分类去重保序；不完整条数与上限照接口', () => {
  const body = deepFreeze(JSON.parse(JSON.stringify(LIST_BODY)));
  const v = U().listView(body);
  assert.deepEqual(v.groups.sfw.map((x) => x.file), ['000_77da87dcd4.gif', '002_bbbbbbbbbb.jpg']);
  assert.deepEqual(v.groups.nsfw.map((x) => x.category), ['分类甲', '分类乙']);
  assert.equal(v.groups.sfw[1].category, null);
  assert.equal(v.groups.sfw[1].size, null);
  assert.equal(v.groups.sfw[1].missing, true);
  assert.deepEqual(v.categories, ['分类甲', '分类乙']);
  assert.deepEqual(v.incomplete, {sfw: 1, nsfw: 0});
  assert.equal(v.maxBytes, 10485760);
  assert.deepEqual(U().listView({}).groups, {sfw: [], nsfw: []});
  assert.equal(U().listView({limits: {max_bytes: -1}}).maxBytes, 10485760);
  assert.equal(U().countsText(v), '普通 2 · 成人 2');
  assert.equal(U().incompleteText(v), '另有不完整的条目（缺标签或图片路径）：普通 1 条。这里不显示，也不会改动它们。');
  assert.equal(U().incompleteText(U().listView({})), '');
});

test('loadPlan：正常 / 库文件坏了（写入不开放）/ 其他失败 / 网络错误', () => {
  assert.deepEqual(U().loadPlan({ok: true, status: 200, body: LIST_BODY}), {ok: true, broken: false, text: ''});
  const broken = U().loadPlan({ok: false, status: 500, body: {ok: false, code: 'library_broken', message: '不是合法的 JSON'}});
  assert.deepEqual(broken, {ok: false, broken: true, text: '表情库文件读不了：不是合法的 JSON 修好之前，这里不能添加、修改或移除。'});
  assert.equal(U().loadPlan({ok: false, status: 500, body: {ok: false, code: 'internal', message: ''}}).text, '读取表情库失败：HTTP 500 请刷新重试。');
  assert.equal(U().loadPlan({ok: false, status: 0, body: null}).text, '读取表情库失败：' + U().NETWORK_FAIL_TEXT + ' 请刷新重试。');
  assert.equal(U().loadPlan({ok: true, status: 200, body: {ok: true}}).ok, false, '没有 groups 当失败');
});

test('filterItems：按标签与分类、不分大小写、多个词都要有；空查询给全部的副本；不改入参', () => {
  const items = deepFreeze([item({label: 'Angry 生气'}), nitem({label: '害羞捂脸', category: '分类甲'}), nitem({label: '开心', category: '分类乙'})]);
  assert.deepEqual(U().filterItems(items, 'angry').map((x) => x.label), ['Angry 生气']);
  assert.deepEqual(U().filterItems(items, '分类').map((x) => x.label), ['害羞捂脸', '开心']);
  assert.deepEqual(U().filterItems(items, ' 分类  乙 ').map((x) => x.label), ['开心']);
  assert.deepEqual(U().filterItems(items, '000_77').map((x) => x.label), [], '文件名不参与');
  const all = U().filterItems(items, '   ');
  assert.deepEqual(all, items);
  assert.notEqual(all, items);
  assert.deepEqual(U().filterItems(null, 'x'), []);
  assert.equal(U().tabCount(1, 3, true), '1/3');
  assert.equal(U().tabCount(1, 3, false), '3');
  assert.equal(U().emptyText('nsfw', ''), '成人这一档还没有表情。');
  assert.equal(U().emptyText('sfw', ' 气 '), '没有符合「气」的表情。');
});

test('缩略图地址与接口地址：两段都做百分号编码；接口给的 img 不在图片路由下就自己拼', () => {
  assert.equal(U().thumbUrl(item()), '/console/stickers/img/sfw/000_77da87dcd4.gif');
  assert.equal(U().thumbUrl(item({img: 'javascript:alert(1)'})), '/console/stickers/img/sfw/000_77da87dcd4.gif');
  assert.equal(U().thumbUrl(item({img: '', file: 'a b#?.gif'})), '/console/stickers/img/sfw/a%20b%23%3F.gif');
  assert.equal(U().itemApiUrl(nitem()), '/api/console/stickers/nsfw/001_aaaaaaaaaa.png');
  assert.equal(U().itemApiUrl(item({file: '../x/y.gif'})), '/api/console/stickers/sfw/..%2Fx%2Fy.gif');
});

test('大小显示与移除确认框（写出这张的标签）', () => {
  assert.deepEqual([0, 1023, 1024, 10232, 1048576, 10485760].map(U().sizeText), ['0 B', '1023 B', '1.0 KB', '10.0 KB', '1.0 MB', '10.0 MB']);
  assert.equal(U().sizeText(null), '');
  assert.equal(U().sizeText(-1), '');
  const t = U().removeConfirmText(item({label: '<b>生气</b> & 2'}));
  assert.ok(t.startsWith('移除「<b>生气</b> & 2」？'));
  assert.ok(t.includes('不会删掉'));
});

// ─────────────────────────────── 结果与汇总
test('addResult：已加入 / 已有（写出库里那条的档和标签）/ 失败附原因', () => {
  assert.deepEqual(U().addResult({ok: true, status: 200, body: {ok: true, item: item()}}), {state: 'added', text: '已加入'});
  assert.deepEqual(U().addResult({ok: false, status: 409, body: {ok: false, code: 'duplicate', message: '库里已有',
    existing: {group: 'nsfw', label: '害羞捂脸'}}}), {state: 'dup', text: '库里已有：成人档「害羞捂脸」'});
  assert.deepEqual(U().addResult({ok: false, status: 422, body: {ok: false, code: 'unsupported_type', message: 'HEIC 不支持，请先转成 JPG'}}),
    {state: 'fail', text: '失败：HEIC 不支持，请先转成 JPG'});
  assert.equal(U().addResult({ok: false, status: 504, body: {ok: false, code: 'timeout'}}).text, '失败：下载超时。');
  assert.equal(U().addResult({ok: false, status: 0, body: null}).text, '失败：' + U().NETWORK_FAIL_TEXT);
  assert.equal(U().addResult({ok: false, status: 413, body: null}).text, '失败：文件太大，超过上限。');
  assert.equal(U().addResult({ok: false, status: 415, body: {error: 'content-type must be application/json'}}).text,
    '失败：' + U().GENERIC_FAIL_TEXT + '（HTTP 415）');
  assert.equal(U().addResult({ok: true, status: 200, body: {ok: false, code: 'constructor'}}).state, 'fail', '响应体的 ok 为准；怪错误码不出错');
});

test('summarize：已加入 / 已有 / 失败的条数与颜色', () => {
  assert.deepEqual(U().summarize(['added', 'added', {state: 'dup'}, {state: 'fail'}, 'wait']),
    {kind: 'warn', text: '已加入 2 张，已有 1 张（库里本来就有，没再加），失败 1 张（原因写在各行）。', added: 2, dup: 1, fail: 1});
  assert.equal(U().summarize(['added']).kind, 'ok');
  assert.equal(U().summarize(['dup']).kind, 'warn');
  assert.equal(U().summarize(['fail', 'fail']).kind, 'err');
  assert.deepEqual(U().summarize([]), {kind: 'warn', text: '没有要加的行。', added: 0, dup: 0, fail: 0});
  assert.equal(U().summarize(null).text, '没有要加的行。');
});

test('待加清单每行的状态、要不要再发、离开提醒、进度', () => {
  assert.deepEqual(['wait', 'busy', 'added', 'dup', 'fail', 'x'].map((s) => U().rowBadge(s).text), ['等待', '上传中', '已加入', '已有', '失败', '等待']);
  assert.deepEqual(['wait', 'busy', 'added', 'dup', 'fail'].map((s) => U().rowBadge(s).kind), ['', 'local', 'ok', 'local', 'bad']);
  assert.deepEqual(['wait', 'busy', 'added', 'dup', 'fail'].map(U().shouldSend), [true, false, false, false, true]);
  assert.deepEqual(['wait', 'busy', 'added', 'dup', 'fail'].map(U().isDone), [false, false, true, true, false]);
  assert.equal(U().needsLeaveWarning([], false), false);
  assert.equal(U().needsLeaveWarning(['added', 'dup'], false), false, '都加完了不提醒');
  assert.equal(U().needsLeaveWarning(['added', 'fail'], false), true);
  assert.equal(U().needsLeaveWarning(['wait'], false), true);
  assert.equal(U().needsLeaveWarning([], true), true, '正在上传时提醒');
  assert.equal(U().progressText(3, 12), '正在加入… 3/12');
});

test('patchResult 与 removeResult：成功、图片没挪走、失败', () => {
  assert.deepEqual(U().patchResult({ok: true, status: 200, body: {ok: true, item: item()}}), {kind: 'ok', text: '已改好。'});
  assert.deepEqual(U().patchResult({ok: false, status: 409, body: {ok: false, code: 'ambiguous'}}),
    {kind: 'err', text: '失败：同一档里有不止一条同名的，没法确定是哪一条。'});
  assert.deepEqual(U().removeResult({ok: true, status: 200, body: {ok: true, moved: true}}, item()), {kind: 'ok', text: '已移除「占位一」。'});
  assert.deepEqual(U().removeResult({ok: true, status: 200, body: {ok: true, moved: false}}, item()),
    {kind: 'warn', text: '已从表情库移除「占位一」，但图片没能挪进回收目录，还留在原处。'});
  assert.deepEqual(U().removeResult({ok: false, status: 404, body: {ok: false, code: 'not_found', message: '没有这一条'}}, item()),
    {kind: 'err', text: '失败：没有这一条'});
  assert.equal(U().removeResult({ok: false, status: 500, body: {ok: false, code: 'library_broken'}}, item()).text, '失败：表情库文件坏了。');
});

test('纯函数不改入参', () => {
  const it = deepFreeze(nitem());
  const inp = deepFreeze({group: 'nsfw', category: '分类甲', label: '新', filename: 'a.png', data: 'AAAA', url: 'https://a.example/x.gif'});
  assert.doesNotThrow(() => {
    U().buildPatchBody(it, inp); U().buildUploadBody(inp); U().buildLinkBody(inp); U().thumbUrl(it); U().itemApiUrl(it);
    U().filterItems(deepFreeze([it]), '新'); U().summarize(deepFreeze(['added'])); U().listView(deepFreeze(JSON.parse(JSON.stringify(LIST_BODY))));
    U().categoryOptions(deepFreeze(['甲'])); U().pickSummary(deepFreeze({added: 1})); U().needsLeaveWarning(deepFreeze(['wait']), false);
  });
});
