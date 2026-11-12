# -*- coding: utf-8 -*-
"""写入的副作用（接口文件 §2；行为清单 14 的写入部分、28）：备份只留 2 份、写到一半失败原文件不变、
库损坏时一切写入被拒、首次创建与权限、两个进程同时添加。全部经 HTTP 接口驱动。"""
import json
import os
import re
import subprocess
import sys
import time

import pytest

import pstk_helpers as K

BACKUP_RE = re.compile(r"stickers\.json\.\d{8}-\d{6}-\d{6}(\.\d+)?")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Windows 不断言权限位")
WRITE_FAIL = "表情库写不进去，文件没有改动"
K_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _backups(paths):
    return sorted(K.listing(paths.backups))


def _first(c):
    """经上传建库并放一张图，返回 item。"""
    return K.ok_item(K.upload(c, K.gif("keep"), label="已有"))


# ── 备份 ─────────────────────────────────────────────────────────────────────

def test_第一次创建目录文件不备份(c, paths):
    _first(c)
    assert not paths.backups.exists() or _backups(paths) == []


def test_之后每次写前备份改动前的字节_只留最新2份_命名合规(c, paths):
    item = _first(c)
    history = []
    for i in range(1, 13):
        history.append(K.raw(paths))
        K.ok_item(c.patch(K.item_api("sfw", item["file"]), json={"label": "占位%d" % i}))
    names = _backups(paths)
    assert len(names) == 2 and all(BACKUP_RE.fullmatch(n) for n in names), names
    assert [(paths.backups / n).read_bytes() for n in names] == history[-2:]   # 按时间先后


@POSIX_ONLY
def test_备份权限同原文件_原文件权限保留(c, paths):
    item = _first(c)
    os.chmod(str(paths.json), 0o640)
    for i in range(3):
        K.ok_item(c.patch(K.item_api("sfw", item["file"]), json={"label": "改%d" % i}))
    names = _backups(paths)
    assert names and all(os.stat(str(paths.backups / n)).st_mode & 0o777 == 0o640 for n in names)
    assert os.stat(str(paths.json)).st_mode & 0o777 == 0o640


def test_备份目录里别的文件不动(c, paths):
    item = _first(c)
    paths.backups.mkdir(exist_ok=True)
    (paths.backups / "notes.txt").write_text("keep")
    (paths.backups / "stickers.json.old").write_text("keep")
    for i in range(12):
        K.ok_item(c.patch(K.item_api("sfw", item["file"]), json={"label": "改%d" % i}))
    assert {"notes.txt", "stickers.json.old"} <= K.listing(paths.backups)
    assert (paths.backups / "notes.txt").read_text() == "keep"
    assert len([n for n in K.listing(paths.backups) if BACKUP_RE.fullmatch(n)]) == 2


# ── 写到一半失败 ─────────────────────────────────────────────────────────────

@pytest.fixture
def replace_fails(paths, monkeypatch):
    """只让"替换成目录文件"这一步失败（目标是 STICKERS_JSON 时抛 OSError），别的替换照常。"""
    real = os.replace
    target = os.path.normcase(os.path.abspath(str(paths.json)))

    def fake(src, dst, *a, **kw):
        if os.path.normcase(os.path.abspath(os.fspath(dst))) == target:
            raise OSError(5, "模拟的写入失败")
        return real(src, dst, *a, **kw)

    def arm():
        monkeypatch.setattr(os, "replace", fake)
    return arm


def test_上传时目录文件替换失败_500_internal_原文件不变_刚写的图被清掉_无临时文件(c, paths, replace_fails):
    first = _first(c)
    before_json, before_imgs = K.raw(paths), K.listing(paths.dir / "sfw")
    replace_fails()
    K.err(K.upload(c, K.png("new"), label="新的"), "internal", 500, message=WRITE_FAIL)
    assert K.raw(paths) == before_json
    assert K.listing(paths.dir / "sfw") == before_imgs
    assert (paths.dir / "sfw" / first["file"]).read_bytes() == K.gif("keep")
    assert K.stray_temps(paths) == []
    assert all(BACKUP_RE.fullmatch(n) for n in K.listing(paths.backups))   # 备份目录里没有半截文件


def test_改标签时目录文件替换失败_500_internal_原文件不变_无临时文件(c, paths, replace_fails):
    item = _first(c)
    before = K.raw(paths)
    replace_fails()
    K.err(c.patch(K.item_api("sfw", item["file"]), json={"label": "新"}), "internal", 500, message=WRITE_FAIL)
    assert K.raw(paths) == before and K.stray_temps(paths) == []


def test_图片那步写不进去_500_internal_目录文件不动_无临时文件(c, paths):
    K.ok_item(K.upload(c, K.gif("keep"), group="nsfw", label="已有", category="占位类"))
    before = K.raw(paths)
    (paths.dir / "sfw").write_bytes(b"occupied by a file")   # sfw 档目录被一个普通文件占着
    K.err(K.upload(c, K.png("new"), label="新的"), "internal", 500, message=WRITE_FAIL)
    assert K.raw(paths) == before and K.stray_temps(paths) == []
    assert (paths.dir / "sfw").read_bytes() == b"occupied by a file"


# ── 库损坏 / 库不存在 ────────────────────────────────────────────────────────

BROKEN = [b'{"sfw": [', b'["sfw"]', b'{"sfw": [], "nsfw": {}}']
BROKEN_IDS = ["json", "array", "nsfw_obj"]


@pytest.fixture
def broken(paths, request):
    paths.json.write_bytes(request.param)
    K.put_image(paths, "sfw", "000_aaaaaaaaaa.gif", K.gif("x"))
    return request.param


def _untouched(paths, content):
    assert paths.json.read_bytes() == content
    assert K.listing(paths.dir / "sfw") == {"000_aaaaaaaaaa.gif"} and not paths.removed.exists()
    assert not paths.backups.exists() and K.stray_temps(paths) == []


@pytest.mark.parametrize("broken", BROKEN, ids=BROKEN_IDS, indirect=True)
def test_库损坏_上传500_library_broken_什么都不动(c, paths, broken):
    K.err(K.upload(c, K.png("p")), "library_broken", 500, prefix=K.BROKEN_HEAD)
    _untouched(paths, broken)


@pytest.mark.parametrize("broken", BROKEN, ids=BROKEN_IDS, indirect=True)
def test_库损坏_PATCH500_library_broken_什么都不动(c, paths, broken):
    K.err(c.patch(K.item_api("sfw", "000_aaaaaaaaaa.gif"), json={"label": "新"}), "library_broken", 500)
    _untouched(paths, broken)


@pytest.mark.parametrize("broken", BROKEN, ids=BROKEN_IDS, indirect=True)
def test_库损坏_DELETE500_library_broken_图不挪(c, paths, broken):
    K.err(c.delete(K.item_api("sfw", "000_aaaaaaaaaa.gif"), json={}), "library_broken", 500)
    _untouched(paths, broken)


def test_库是坏JSON_链接接口500_library_broken_文案含不是合法的JSON(c, paths):
    paths.json.write_bytes(b'{"sfw": [}')
    K.err(K.link(c, "https://pics.stk-test.example/a.png"), "library_broken", 500, contains="不是合法的 JSON")
    assert paths.json.read_bytes() == b'{"sfw": [}'


@POSIX_ONLY
def test_库不存在时第一次添加创建它_权限0644_不受umask影响(c, paths):
    old = os.umask(0o077)
    try:
        item = K.ok_item(K.upload(c, K.webp("first"), group="nsfw", label="占位", category="占位类"))
    finally:
        os.umask(old)
    assert os.stat(str(paths.json)).st_mode & 0o777 == 0o644
    assert os.stat(str(paths.dir / "nsfw" / item["file"])).st_mode & 0o777 == 0o644


def test_库不存在时第一次添加_内容只含两档与新条目_结尾无换行(c, paths):
    data = K.webp("first")
    item = K.ok_item(K.upload(c, data, group="nsfw", label="占位", category="占位类"))
    expected = {"sfw": [], "nsfw": [{"url": "", "label": "占位", "category": "占位类",
                                     "path": str(paths.dir / "nsfw" / item["file"]), "size": len(data)}]}
    assert K.raw(paths) == K.dumps(expected).encode("utf-8")


def test_目录文件所在目录不存在时一并建好(c, paths, monkeypatch):
    target = paths.tmp / "deep" / "er" / "stickers.json"
    monkeypatch.setenv("STICKERS_JSON", str(target))
    K.ok_item(K.upload(c, K.gif("deep")))
    assert json.loads(target.read_text(encoding="utf-8"))["sfw"][0]["label"] == "占位"


# ── 两个进程同时添加 ─────────────────────────────────────────────────────────

_CHILD = r"""
import os, sys, time, base64
sys.path.insert(0, sys.argv[1])
from moments import panel_auth, web
go, tag, secrets = sys.argv[2], sys.argv[3], sys.argv[4]
panel_auth.SECRET_PATH = os.path.join(secrets, "panel.env")
panel_auth.SESSIONS_PATH = os.path.join(secrets, "sessions.json")
panel_auth.set_password("child-pw-12345")
web.app.config["TESTING"] = True
c = web.app.test_client()
assert c.post("/login", data={"password": "child-pw-12345", "next": "/"}).status_code == 302
datas = [b"GIF89a" + (("|%s-%d" % (tag, i)).encode() * 20) for i in range(3)]
while not os.path.exists(go):
    time.sleep(0.002)
for i, d in enumerate(datas):
    r = c.post("/api/console/stickers/upload", json={"group": "sfw", "label": "%s-%d" % (tag, i),
               "filename": "a.gif", "data": base64.b64encode(d).decode()})
    assert r.status_code == 200, r.get_data(as_text=True)
    print(r.get_json()["item"]["file"], flush=True)
"""


def test_两个进程同时添加_条目都在_文件名两两不同_JSON合法(paths):
    go = paths.tmp / "go"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    procs, outs = [], []
    try:
        for tag in ("甲", "乙"):
            secrets = paths.tmp / ("secrets-" + tag)
            secrets.mkdir()
            procs.append(subprocess.Popen([sys.executable, "-c", _CHILD, str(K_REPO), str(go), tag, str(secrets)],
                                          cwd=str(paths.tmp), env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        time.sleep(1.0)   # 两个都起来、都在等开始信号
        go.write_text("go")
        for p in procs:
            out, err = p.communicate(timeout=60)
            assert p.returncode == 0, err.decode("utf-8", "replace")[-2000:]
            outs += out.decode("utf-8").split()
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
    lib = K.load(paths)
    names = [K.base(e["path"]) for e in lib["sfw"]]
    assert sorted(names) == sorted(outs) and len(set(names)) == 6
    assert sorted(e["label"] for e in lib["sfw"]) == sorted("%s-%d" % (t, i) for t in ("甲", "乙") for i in range(3))
    assert K.listing(paths.dir / "sfw") == set(names) and K.stray_temps(paths) == []

