# -*- coding: utf-8 -*-
"""朋友圈页（公开版）验收的公共夹具。只依据接口文件写，不读实现代码。

- 每条用例：临时 HOME；朋友圈库 db.DB_PATH 换成临时文件并先建表；图片根 MOMENTS_MEDIA_ROOT、全局参数
  CLAUDEBOT_GLOBAL_CFG 指到临时目录（全局参数文件缺省不建 → 显示名"我"）；代理环境变量清掉。
- 底层适配器一律走替身：adapters.registry.enabled_backends / adapter 换成登记表（缺省没有任何底层），
  不连任何 bot、不连任何端口。
- 管理台壳用进程内 Flask test_client；口令与会话文件钉在临时目录。
- 被测模块 moments.moments_routes 只在夹具里导入：还没实现时整套照样能被收集，运行时失败（红态）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pmom_helpers as H  # noqa: E402

PASSWORD = "test-panel-pw-mom-12345"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy",
              "NO_PROXY", "no_proxy")


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    for k in PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    for k in ("MOMENTS_WEB_MODE", "MOMENTS_WEB_PORT", "MOMENTS_WEB_HOST", "CONSOLE_BACKENDS"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("MOMENTS_MEDIA_ROOT", str(media))
    gcfg = tmp_path / "global.yml"
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(gcfg))
    db_path = tmp_path / "db" / "moments.db"
    db_path.parent.mkdir()
    import db
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init()
    return SimpleNamespace(tmp=tmp_path, home=home, media=media, gcfg=gcfg, db_path=db_path, db=db,
                           user_auto=media / "__user__" / "auto", user_comments=media / "__user__" / "comments",
                           user_profile=media / "__user__" / "profile")


@pytest.fixture(autouse=True)
def backends(monkeypatch):
    """底层登记表替身；缺省一个底层都没有。用例里 backends.add("claude", bots=[...]) 加。"""
    from adapters import registry
    reg = H.Backends()
    monkeypatch.setattr(registry, "enabled_backends", reg.enabled)
    monkeypatch.setattr(registry, "adapter", reg.adapter)
    yield reg
    for f in reg.fakes.values():
        if f.block is not None:
            f.block.set()


@pytest.fixture
def mr():
    """被测模块（接口文件 §0 的 BOTS_TIMEOUT 所在）。还没实现时这里 ImportError，用例记为失败。"""
    from moments import moments_routes
    return moments_routes


@pytest.fixture
def app(paths, monkeypatch):
    from moments import panel_auth, web
    secrets = paths.tmp / "secrets"
    monkeypatch.setattr(panel_auth, "SECRET_PATH", str(secrets / "moments-panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH", str(secrets / "moments-panel-sessions.json"))
    panel_auth._fail.clear()
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True
    yield web.app
    panel_auth._fail.clear()


@pytest.fixture
def anon(app):
    """未登录的客户端。"""
    return app.test_client()


def login(app):
    cl = app.test_client()
    r = cl.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    return cl


@pytest.fixture
def c(app):
    """已登录的客户端。"""
    return login(app)


@pytest.fixture
def new_client(app):
    """再要一个独立登录的客户端（并发用例用）。"""
    return lambda: login(app)


@pytest.fixture
def set_global(paths):
    """写全局参数文件（YAML 映射）。"""
    def _w(**kv):
        lines = ['%s: "%s"' % (k, v) for k, v in kv.items()]
        paths.gcfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return _w
