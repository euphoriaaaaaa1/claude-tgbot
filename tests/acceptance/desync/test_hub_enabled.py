"""需求⑤（公开管理台）：POST /hub/api/bots/<id>/enabled 响应契约（INTERFACE §10.6）。
Flask test client + HUB_CONFIGS_DIR；subprocess 全部打桩为抛错（r5 不调 launchctl，调了就失败）。
Flask app 对象名契约未给：在 moments.web 模块属性里找 Flask 实例。
"""
import os
import subprocess

import pytest

from test_producers_enabled import _mw, cfg


@pytest.fixture
def hub(iso, registry, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess 被调用（r5 不得调 launchctl 等）")
    for n in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, n, boom)
    from flask import Flask
    w = _mw(registry)
    apps = [v for v in vars(w).values() if isinstance(v, Flask)]
    assert apps, "moments.web 未暴露 Flask app"
    apps[0].config["TESTING"] = True
    return apps[0].test_client()


def _yml(iso, bot):
    return open(os.path.join(iso["cfg"], f"{bot}.yml"), "rb").read()


def test_enabled_false_200_ok_enabled_restart_hint三键_yml变为false(iso, registry, hub):
    cfg(iso, "bot2", True)
    r = hub.post("/hub/api/bots/bot2/enabled", json={"enabled": False})
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["ok"] is True and body["enabled"] is False and "restart_hint" in body
    assert b"enabled: false" in _yml(iso, "bot2")
    assert "bot2" in registry.disabled_ids_safe()


def test_enabled_true_200_恢复_yml为true_不在停止集合(iso, registry, hub):
    cfg(iso, "bot2", False)
    r = hub.post("/hub/api/bots/bot2/enabled", json={"enabled": True})
    assert r.status_code == 200 and r.get_json()["enabled"] is True
    assert b"enabled: true" in _yml(iso, "bot2")
    assert "bot2" not in registry.disabled_ids_safe()


def test_enabled_非布尔_400_bad_body_yml不变(iso, registry, hub):
    cfg(iso, "bot2", True)
    before = _yml(iso, "bot2")
    r = hub.post("/hub/api/bots/bot2/enabled", json={"enabled": "no"})
    # r7 §10.6 定死：既有 HubError 形状 {"error","detail"}，无 ok 键
    body = r.get_json()
    assert r.status_code == 400 and body["error"] == "bad_body"
    assert "ok" not in body and "detail" in body
    assert _yml(iso, "bot2") == before


def test_enabled_缺键或非JSON_400_yml不变(iso, registry, hub):
    cfg(iso, "bot2", True)
    before = _yml(iso, "bot2")
    for r in (hub.post("/hub/api/bots/bot2/enabled", json={}),
              hub.post("/hub/api/bots/bot2/enabled", data="not json", content_type="application/json")):
        assert r.status_code == 400 and r.get_json()["error"] == "bad_body" and "ok" not in r.get_json()
    assert _yml(iso, "bot2") == before


def test_enabled_bot不存在_404(iso, registry, hub):
    r = hub.post("/hub/api/bots/ghost/enabled", json={"enabled": True})
    assert r.status_code == 404


def test_enabled_重复同值_响应相同_yml字节不变(iso, registry, hub):
    cfg(iso, "bot2", True)
    r1 = hub.post("/hub/api/bots/bot2/enabled", json={"enabled": False})
    after1 = _yml(iso, "bot2")
    r2 = hub.post("/hub/api/bots/bot2/enabled", json={"enabled": False})
    assert (r1.status_code, r1.get_json()) == (r2.status_code, r2.get_json())
    assert r1.status_code == 200 and _yml(iso, "bot2") == after1


def test_enabled_中文bot名_不5xx_且非200时yml不变(iso, registry, hub):
    # 契约未定 bot id 字符集：只要求不崩（非 5xx）；若拒绝则 yml 字节不变，若接受则 yml 变为 false
    cfg(iso, "陈璐璐", True)
    before = _yml(iso, "陈璐璐")
    r = hub.post("/hub/api/bots/陈璐璐/enabled", json={"enabled": False})
    assert r.status_code < 500, r.data
    assert (r.status_code == 200 and b"enabled: false" in _yml(iso, "陈璐璐")) or _yml(iso, "陈璐璐") == before
