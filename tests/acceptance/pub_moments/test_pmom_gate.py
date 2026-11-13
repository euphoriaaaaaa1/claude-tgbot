# -*- coding: utf-8 -*-
"""登录门、跨站核对、可达面（接口文件 §0；行为清单第 1–8、17 条）。"""
import os
import subprocess
import sys
import textwrap

import pytest

import pmom_helpers as H

API_ALL = [("GET", "/api/moments"), ("POST", "/api/like"), ("POST", "/api/comment"), ("POST", "/api/moment"),
           ("DELETE", "/api/comment/1"), ("DELETE", "/api/moment/1"), ("GET", "/api/profile/someone"),
           ("POST", "/api/profile/someone"), ("GET", "/api/moment/1")]
WRITES = [("POST", "/api/like"), ("POST", "/api/comment"), ("POST", "/api/moment"), ("DELETE", "/api/comment/1"),
          ("DELETE", "/api/moment/1"), ("POST", "/api/profile/someone")]
EVIL = "https://evil.example"


def _body_for(path, mid):
    if path == "/api/like":
        return {"moment_id": mid}
    if path == "/api/comment":
        return {"moment_id": mid, "text": "闸门评论"}
    if path == "/api/moment":
        return {"text": "闸门动态", "image": H.data_url(H.png("gate"))}
    if path.startswith("/api/profile/"):
        return {"display_name": "闸门名", "avatar_url": H.data_url(H.png("gate-av"))}
    return {}


def _seed(paths, backends):
    """一条用户动态（编号 1）+ 一条用户评论（编号 1）+ 一个在跑的人，用来看"没写库、没投递"。"""
    backends.add("claude", bots=[H.bot("anna")])
    mid = H.user_moment(paths.db, "闸门前的动态")
    assert mid == 1
    cid = H.add_comment(paths.db_path, mid, H.ME, "闸门前的评论")
    assert cid == 1
    return mid


# ── 未登录 ───────────────────────────────────────────────────────────────────

def test_未登录打开朋友圈页_302到登录页_next带原查询串(anon):
    r = anon.get("/moments?bot=x")
    assert r.status_code == 302
    assert r.headers["Location"] == "/login?next=/moments%3Fbot%3Dx"


def test_未登录取图片_302到登录页(anon):
    r = anon.get("/image/a.png")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login?next=")


@pytest.mark.parametrize("method,path", API_ALL)
def test_未登录调接口_401_库与图片目录不变(anon, paths, backends, method, path):
    mid = _seed(paths, backends)
    before = (H.tables(paths.db_path), H.media_state(paths.media))
    r = anon.open(path, method=method, json=_body_for(path, mid))
    assert (r.status_code, r.get_json(silent=True)) == (401, {"error": "unauthorized"})
    assert (H.tables(paths.db_path), H.media_state(paths.media)) == before
    assert backends.calls == []


# ── 跨站与内容类型 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", WRITES)
def test_外站Origin写请求_403_不写库不投递(c, paths, backends, method, path):
    mid = _seed(paths, backends)
    before = (H.tables(paths.db_path), H.media_state(paths.media))
    r = c.open(path, method=method, json=_body_for(path, mid), headers={"Origin": EVIL})
    assert (r.status_code, r.get_json(silent=True)) == (403, {"error": "cross-site request rejected"})
    assert (H.tables(paths.db_path), H.media_state(paths.media)) == before
    assert backends.calls == []


@pytest.mark.parametrize("method,path", WRITES)
def test_外站Referer写请求_403_不写库不投递(c, paths, backends, method, path):
    mid = _seed(paths, backends)
    before = (H.tables(paths.db_path), H.media_state(paths.media))
    r = c.open(path, method=method, json=_body_for(path, mid), headers={"Referer": EVIL + "/moments"})
    assert (r.status_code, r.get_json(silent=True)) == (403, {"error": "cross-site request rejected"})
    assert (H.tables(paths.db_path), H.media_state(paths.media)) == before
    assert backends.calls == []


@pytest.mark.parametrize("method,path", WRITES)
def test_同站但不是JSON_415_不写库不投递(c, paths, backends, method, path):
    mid = _seed(paths, backends)
    before = (H.tables(paths.db_path), H.media_state(paths.media))
    r = c.open(path, method=method, data=H.jdump(_body_for(path, mid)), content_type="text/plain",
               headers={"Origin": "http://localhost"})
    assert (r.status_code, r.get_json(silent=True)) == (415, {"error": "content-type must be application/json"})
    assert (H.tables(paths.db_path), H.media_state(paths.media)) == before
    assert backends.calls == []


# ── 登录后可达 ───────────────────────────────────────────────────────────────

def test_登录后打开朋友圈页_200_HTML(c):
    r = c.get("/moments")
    assert r.status_code == 200 and r.mimetype == "text/html"


def test_登录后动态列表接口可达_空库回空数组(c):
    r = c.get("/api/moments")
    assert (r.status_code, r.get_json()) == (200, [])


def test_登录后资料接口可达(c):
    r = c.get("/api/profile/someone")
    assert r.status_code == 200 and set(r.get_json()) == H.PROFILE_KEYS


def test_登录后动态详情接口可达(c, paths):
    mid = H.user_moment(paths.db, "详情可达")
    r = c.get("/api/moment/%d" % mid)
    assert r.status_code == 200 and r.get_json()["text"] == "详情可达"


def test_登录后同站来源发动态再按编号查得到(c):
    r = c.post("/api/moment", json={"text": "hi"}, headers={"Origin": "http://localhost"})
    assert r.status_code == 200
    assert c.get("/api/moment/%d" % r.get_json()["id"]).get_json()["text"] == "hi"


@pytest.mark.parametrize("method,path", [("GET", "/api/moments/image_engine"), ("POST", "/api/moments/image_engine"),
                                         ("GET", "/api/image_provider"), ("POST", "/api/image_provider")])
def test_不搬的生图引擎接口_登录后404_not_found(c, method, path):
    r = c.open(path, method=method, json={})
    assert (r.status_code, r.get_json(silent=True)) == (404, {"error": "not_found"})


def test_删评论编号不是整数_404_not_found(c):
    r = c.delete("/api/comment/abc", json={})
    assert (r.status_code, r.get_json(silent=True)) == (404, {"error": "not_found"})


def test_查动态编号不是整数_404_not_found(c):
    r = c.get("/api/moment/abc")
    assert (r.status_code, r.get_json(silent=True)) == (404, {"error": "not_found"})


def test_点赞接口用GET_405_method_not_allowed(c):
    r = c.get("/api/like")
    assert (r.status_code, r.get_json(silent=True)) == (405, {"error": "method_not_allowed"})


# ── 库文件不存在时（D3）：新进程里第一次打开页面就建表 ───────────────────────

_FRESH = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from adapters import registry
    registry.enabled_backends = lambda: []
    from moments import panel_auth, web
    panel_auth.SECRET_PATH = os.path.join(sys.argv[2], "panel.env")
    panel_auth.SESSIONS_PATH = os.path.join(sys.argv[2], "sessions.json")
    panel_auth.set_password("fresh-proc-pw-12345")
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    assert c.post("/login", data={"password": "fresh-proc-pw-12345", "next": "/"}).status_code == 302
    print("STATUS=%d" % c.get("/moments").status_code)
""")


def test_库文件不存在_新进程登录后打开页面200_之后库里有朋友圈四张表(paths):
    script = paths.tmp / "fresh_proc.py"
    script.write_text(_FRESH, encoding="utf-8")
    secrets = paths.tmp / "fresh-secrets"
    secrets.mkdir()
    db_file = paths.tmp / "fresh" / "nested" / "moments.db"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    env.update(HOME=str(paths.home), USERPROFILE=str(paths.home), CLAUDEBOT_DB_PATH=str(db_file),
               MOMENTS_MEDIA_ROOT=str(paths.media), CLAUDEBOT_GLOBAL_CFG=str(paths.gcfg))
    cp = subprocess.run([sys.executable, str(script), str(H.REPO), str(secrets)], env=env, cwd=str(paths.tmp),
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    out = cp.stdout.decode("utf-8", "replace")
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")[-1500:]
    assert "STATUS=200" in out, out
    names = {r["name"] for r in H.q(db_file, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"moments", "moment_likes", "moment_comments", "bot_profiles"} <= names, sorted(names)
