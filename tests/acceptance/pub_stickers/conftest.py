# -*- coding: utf-8 -*-
"""表情包管理页（公开版）验收的公共夹具。只依据接口文件写，不读实现代码。

- 每条用例：临时 HOME；STICKERS_JSON / STICKERS_DIR 指到本用例的临时目录；代理环境变量全部清掉
  （不清的话回环请求会走本机代理）。
- 管理台壳用进程内 Flask test_client；口令与会话文件钉在临时目录，不读真实家目录。
- 被测模块 moments.sticker_store 只在夹具里导入：模块还不存在时整套照样能被收集，运行时失败（红态）。
- 假图片服务器只监听 127.0.0.1 的端口 0，不连外网、不连任何固定端口。
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pstk_helpers as K  # noqa: E402

PASSWORD = "test-panel-pw-stk-12345"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy",
              "NO_PROXY", "no_proxy")
REAL_HOME = os.path.expanduser("~")   # 在改 HOME 之前定住，只用来断言"没有指过去"


def _under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    """表情库指到本用例的临时目录：目录文件所在目录先建好（文件本身不建），图片根先不建。"""
    for k in PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    shared = tmp_path / "stk-shared"
    shared.mkdir()
    img = tmp_path / "stk-img"
    monkeypatch.setenv("STICKERS_JSON", str(shared / "stickers.json"))
    monkeypatch.setenv("STICKERS_DIR", str(img))
    for k in ("STICKERS_JSON", "STICKERS_DIR"):
        assert _under(os.environ[k], str(tmp_path)), "%s 不在本用例的临时目录下" % k
        assert not _under(os.environ[k], os.path.join(REAL_HOME, ".claude")), "%s 指到了真实家目录" % k
    return SimpleNamespace(tmp=tmp_path, home=home, json=shared / "stickers.json", shared=shared, dir=img,
                           backups=shared / ".stickers-backups", removed=img / ".removed")


@pytest.fixture
def store():
    """被测模块（接口文件 §0 的测试挂点所在）。还没实现时这里 ImportError，用例记为失败。"""
    from moments import sticker_store
    return sticker_store


@pytest.fixture
def app(paths, monkeypatch):
    from moments import panel_auth, web
    secrets = paths.tmp / "secrets"
    monkeypatch.setattr(panel_auth, "SECRET_PATH", str(secrets / "moments-panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH", str(secrets / "moments-panel-sessions.json"))
    for k in ("MOMENTS_WEB_MODE", "MOMENTS_WEB_PORT", "MOMENTS_WEB_HOST"):
        monkeypatch.delenv(k, raising=False)
    panel_auth._fail.clear()
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True
    yield web.app
    panel_auth._fail.clear()


@pytest.fixture
def anon(app):
    """未登录的客户端。"""
    return app.test_client()


@pytest.fixture
def c(app):
    """已登录的客户端。"""
    cl = app.test_client()
    r = cl.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    return cl


@pytest.fixture
def fake():
    """只起假图片服务器，不替换任何挂点。"""
    f = K.Fake()
    yield f
    f.close()


@pytest.fixture
def net(fake, store, monkeypatch):
    """假服务器 + 两个替身：解析由替身给出公网样地址，打开连接改连假服务器（不走代理）。"""
    fake.no_redirect = store._NoRedirect
    fake.real_open = store.open_url
    monkeypatch.setattr(store, "resolve", fake.resolve)
    monkeypatch.setattr(store, "open_url", fake.open)
    return fake


@pytest.fixture
def sample(paths):
    lib = K.sample_library(paths)
    K.write_lib(paths, lib)
    return lib
