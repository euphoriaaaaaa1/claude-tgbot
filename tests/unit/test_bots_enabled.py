# -*- coding: utf-8 -*-
"""`configs/<bot>.yml` 顶层 `enabled` 的停用开关 —— 判定层 + 写开关端点。

判定层只碰 tmp_path 下的假 configs（`HUB_CONFIGS_DIR` / `config_loader.CONFIGS_DIR`
两个接缝），绝不读仓库真 configs/。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import bots_registry
import config_loader

REPO_ROOT = Path(bots_registry.__file__).resolve().parent


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "configs"
    d.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(d))
    monkeypatch.setattr(config_loader, "CONFIGS_DIR", str(d))
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)
    return d


def write_bot(d, bot_id, **fields):
    lines = [f"id: {bot_id}", "display_name: 某个 bot"]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    (d / f"{bot_id}.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------ 判定（缺省 / 显式）

def test_没写enabled字段的存量bot照常启用(cfgdir):
    """默认值是"启用"，不然升级到这版的机器上全部 bot 一夜之间集体停摆。"""
    write_bot(cfgdir, "alpha")
    assert [b["id"] for b in bots_registry.bots()] == ["alpha"]
    assert bots_registry.disabled_ids() == set()


def test_显式false的bot不进注册表(cfgdir):
    write_bot(cfgdir, "alpha")
    write_bot(cfgdir, "beta", enabled="false")
    assert [b["id"] for b in bots_registry.bots()] == ["alpha"]
    assert bots_registry.disabled_ids() == {"beta"}


@pytest.mark.parametrize("raw", ["false", "no", "0", "off", '""'])
def test_各种写法的假值都算停用(cfgdir, raw):
    """手写 `enabled: 0` 的人要的就是停 —— 按 `is False` 判会把它当启用。"""
    write_bot(cfgdir, "beta", enabled=raw)
    assert bots_registry.bots() == []


@pytest.mark.parametrize("raw", ["true", "yes", "1"])
def test_真值照常启用(cfgdir, raw):
    write_bot(cfgdir, "beta", enabled=raw)
    assert [b["id"] for b in bots_registry.bots()] == ["beta"]


def test_include_disabled能拿回停用的bot(cfgdir):
    write_bot(cfgdir, "beta", enabled="false")
    got = bots_registry.bots(include_disabled=True)
    assert [b["id"] for b in got] == ["beta"]
    # 行的字段集是 §6 契约，绝不因为这个开关多长一个键出来
    assert set(got[0]) == {"id", "display_name", "port", "namespace_uuid", "port_derived"}


def test_停用一个bot不会让别人的派生端口漂移(cfgdir):
    """端口按全表解析：停用的 bot 仍占着号，否则重新启用时端口就换人了。"""
    write_bot(cfgdir, "aaa")
    write_bot(cfgdir, "bbb")
    before = bots_registry.ports()
    write_bot(cfgdir, "aaa", enabled="false")
    after = bots_registry.ports()
    assert after == before, "停用后端口表变了：%s → %s" % (before, after)


def test_停用的bot仍查得到端口与命名空间(cfgdir):
    """通讯录语义：查不到端口 = 管理台把它显示成 unknown 而不是 offline。"""
    write_bot(cfgdir, "beta", enabled="false", dispatcher_port=17888,
              namespace_uuid="22222222-2222-2222-2222-222222222222")
    assert bots_registry.ports()["beta"] == 17888
    assert bots_registry.namespace_for("beta").endswith("222222222222")


def test_CLI不吐停用的bot(cfgdir):
    """重启脚本 / Windows 启动脚本全按 CLI 拉起 —— 这一条就是"停了不被拉回来"。"""
    write_bot(cfgdir, "alpha")
    write_bot(cfgdir, "beta", enabled="false")
    env = dict(os.environ, HUB_CONFIGS_DIR=str(cfgdir))
    p = subprocess.run([sys.executable, "-m", "bots_registry", "--format=sh"],
                       cwd=str(REPO_ROOT), env=env, capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert [ln.split("\t")[0] for ln in p.stdout.strip().split("\n")] == ["alpha"]
    js = subprocess.run([sys.executable, "-m", "bots_registry", "--format=json"],
                        cwd=str(REPO_ROOT), env=env, capture_output=True,
                        text=True, timeout=60)
    assert [b["id"] for b in json.loads(js.stdout)] == ["alpha"]


def test_全部bot被停用时CLI退出码1(cfgdir):
    """空名单 = 1：启动脚本据此报错，而不是静默起 0 个 bot。"""
    write_bot(cfgdir, "beta", enabled="false")
    p = subprocess.run([sys.executable, "-m", "bots_registry", "--format=sh"],
                       cwd=str(REPO_ROOT), env=dict(os.environ, HUB_CONFIGS_DIR=str(cfgdir)),
                       capture_output=True, text=True, timeout=60)
    assert (p.returncode, p.stdout.strip()) == (1, "")


# ------------------------------------------------------------ config_loader 同口径

def test_list_enabled_bots跳过停用的(cfgdir):
    write_bot(cfgdir, "alpha")
    write_bot(cfgdir, "beta", enabled="false")
    assert [c["_bot_id"] for c in config_loader.list_enabled_bots()] == ["alpha"]


def test_list_enabled_bots的include_disabled留住停用的(cfgdir):
    """管理台是唯一能把它启用回来的地方，从列表里消失就再也点不回来了。"""
    write_bot(cfgdir, "alpha")
    write_bot(cfgdir, "beta", enabled="false")
    got = [c["_bot_id"] for c in config_loader.list_enabled_bots(include_disabled=True)]
    assert got == ["alpha", "beta"]


# ------------------------------------------------------------ 写开关端点

@pytest.fixture
def client(cfgdir, monkeypatch):
    from flask import Flask

    from moments.hub_routes import hub_bp
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(cfgdir.parent / "channels"))
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", "http://127.0.0.1:9")
    app = Flask(__name__)
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


#: 带注释、锚点、块标量、行尾注释的一份 bot yml —— 保真断言的靶子
RICH_YML = """\
# 陈露露的人设
id: chenlulu
display_name: 陈露露          # 行尾注释
dispatcher_port: 17801
persona_summary: |
  端庄得体、冷淡慢热。
  绝不主动。
sleep_hours: ["00:00-07:30"]
moments:
  enabled: false
"""


def put_yml(cfgdir, bot_id, text):
    (cfgdir / (bot_id + ".yml")).write_text(text, encoding="utf-8")


def read_yml(cfgdir, bot_id):
    return (cfgdir / (bot_id + ".yml")).read_text(encoding="utf-8")


def test_停用写进yml且被注册表认出来(client, cfgdir):
    put_yml(cfgdir, "chenlulu", RICH_YML)
    r = client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": False})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["enabled"] is False
    assert bots_registry.bots() == []
    assert bots_registry.disabled_ids() == {"chenlulu"}


def test_启用回来(client, cfgdir):
    put_yml(cfgdir, "chenlulu", RICH_YML + "enabled: false\n")
    assert bots_registry.bots() == []
    r = client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": True})
    assert r.status_code == 200
    assert [b["id"] for b in bots_registry.bots()] == ["chenlulu"]


def test_写入保真_注释锚点块标量键序一字不动(client, cfgdir):
    """bot 的 yml 里全是人设正文，dump 一次就毁 —— 只许换那一段字节。"""
    put_yml(cfgdir, "chenlulu", RICH_YML)
    client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": False})
    got = read_yml(cfgdir, "chenlulu")
    assert "# 陈露露的人设" in got
    assert "# 行尾注释" in got
    assert "  端庄得体、冷淡慢热。\n  绝不主动。\n" in got
    assert 'sleep_hours: ["00:00-07:30"]' in got
    # 只多了那一行，别的一个字节没动
    added = [ln for ln in got.splitlines() if ln not in RICH_YML.splitlines()]
    assert added == ["enabled: false"], added
    # 顶层 enabled 与 moments.enabled 是两码事，后者不许被顺手改
    import yaml as _y
    assert _y.safe_load(got)["moments"]["enabled"] is False


def test_幂等_重复停用产出同一份文件(client, cfgdir):
    put_yml(cfgdir, "chenlulu", RICH_YML)
    client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": False})
    once = read_yml(cfgdir, "chenlulu")
    client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": False})
    assert read_yml(cfgdir, "chenlulu") == once


def test_已有enabled键时就地改而不是再插一行(client, cfgdir):
    put_yml(cfgdir, "chenlulu", "id: chenlulu\nenabled: true   # 开着\ndisplay_name: 陈\n")
    client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": False})
    got = read_yml(cfgdir, "chenlulu")
    assert got == "id: chenlulu\nenabled: false   # 开着\ndisplay_name: 陈\n"


def test_不存在的bot回404(client, cfgdir):
    r = client.post("/hub/api/bots/nosuchbot/enabled", json={"enabled": False})
    assert r.status_code == 404
    assert r.get_json()["error"] == "bot_not_found"


@pytest.mark.parametrize("bad", ["_global", "Chenlulu", "a", "有中文",
                                 "x" * 33, "1abc"])
def test_非法bot_id回400而不是去碰磁盘(client, cfgdir, bad):
    """id 会拼进文件名 —— 形状校验与建 bot 共用同一个 BOT_ID_RE。"""
    r = client.post("/hub/api/bots/%s/enabled" % bad, json={"enabled": False})
    assert r.status_code == 400, (bad, r.status_code)
    assert r.get_json()["error"] == "bad_bot_id"


def test_路径穿越拿不到仓库外的文件(client, cfgdir, tmp_path):
    victim = tmp_path / "victim.yml"
    victim.write_text("id: victim\n", encoding="utf-8")
    r = client.post("/hub/api/bots/..%2f..%2fvictim/enabled", json={"enabled": False})
    assert r.status_code in (400, 404), r.status_code
    assert victim.read_text(encoding="utf-8") == "id: victim\n"


@pytest.mark.parametrize("body", [{}, {"enabled": "false"}, {"enabled": 0},
                                  {"enabled": None}, {"enabled": "true"}])
def test_enabled不是布尔一律400(client, cfgdir, body):
    """`"false"` 按 JS 真值判会变成"点了停止反而是启用"——只认真布尔。"""
    put_yml(cfgdir, "chenlulu", RICH_YML)
    r = client.post("/hub/api/bots/chenlulu/enabled", json=body)
    assert r.status_code == 400, (body, r.status_code)
    assert r.get_json()["error"] == "bad_body"
    # 顶层没被写出 enabled 键（`moments.enabled` 是另一码事，本来就在文件里）
    import yaml as _y
    assert "enabled" not in _y.safe_load(read_yml(cfgdir, "chenlulu"))


def test_请求体不是对象回400_bad_body(client, cfgdir):
    put_yml(cfgdir, "chenlulu", RICH_YML)
    r = client.post("/hub/api/bots/chenlulu/enabled", data="[1,2,3]",
                    content_type="application/json")
    assert r.status_code == 400 and r.get_json()["error"] == "bad_body"


def test_坏yml回409而不是把文件写坏(client, cfgdir):
    put_yml(cfgdir, "chenlulu", "id: [oops\n  x: : :\n")
    before = read_yml(cfgdir, "chenlulu")
    r = client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": False})
    assert r.status_code == 409 and r.get_json()["error"] == "bad_yaml"
    assert read_yml(cfgdir, "chenlulu") == before
    assert str(cfgdir) not in json.dumps(r.get_json(), ensure_ascii=False)


def test_响应带重启提示(client, cfgdir):
    """停用不自动重启（会打断所有 bot 的对话），页面据此复用一期的重启入口。"""
    put_yml(cfgdir, "chenlulu", RICH_YML)
    body = client.post("/hub/api/bots/chenlulu/enabled",
                       json={"enabled": False}).get_json()
    assert body["restart_hint"]["bot"] is True


def test_列表接口把停用的bot留在页面上并另列disabled(client, cfgdir):
    """从列表里消失 = 停了以后再也点不回来。"""
    put_yml(cfgdir, "chenlulu", RICH_YML)
    put_yml(cfgdir, "xiaoyu", "id: xiaoyu\ndisplay_name: 小雨\ndispatcher_port: 17802\n")
    client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": False})
    body = client.get("/hub/api/bots").get_json()
    assert sorted(b["id"] for b in body["bots"]) == ["chenlulu", "xiaoyu"]
    assert body["disabled"] == ["chenlulu"]
    # §6.1 的行字段集不许因为这个功能变形
    assert set(body["bots"][0]) == {"id", "display_name", "port", "state", "phase",
                                    "pid", "queue_depth", "in_flight"}


def test_不留临时文件(client, cfgdir):
    put_yml(cfgdir, "chenlulu", RICH_YML)
    client.post("/hub/api/bots/chenlulu/enabled", json={"enabled": False})
    assert list(cfgdir.glob("*.tmp")) == []
    assert [p.name for p in cfgdir.iterdir()] == ["chenlulu.yml"]


# ------------------------------------------------------------ 重启脚本得杀得掉停用的 bot

def test_重启脚本先停全部tg会话再按注册表拉起():
    """停用的 bot 已经不在注册表里，循环轮不到它 —— 只在循环内逐个 kill 等于没停。

    源码级断言（同 tests/acceptance/hub2/test_source_contracts.py 的口径）：
    真跑一遍会把机主正在用的 bot 全停掉。
    """
    txt = (REPO_ROOT / "restart-bots.example.sh").read_text(encoding="utf-8")
    sweep = txt.find("tmux kill-session")
    loop = txt.find("while IFS=")
    read_registry = txt.find("bots_registry --format=sh")
    assert sweep != -1, "重启脚本不停旧会话，停用的 bot 会一直活着"
    assert "tg-[a-z0-9_]" in txt, "没有按 tg-* 前缀扫会话，漏掉的正是已从注册表消失的那些"
    assert read_registry < sweep < loop, \
        "停旧会话必须排在『读注册表成功之后、拉起之前』（读不出名单时不该白停一遍）"
    assert txt.count("tmux kill-session") == 1, \
        "循环里还留着逐个 kill —— 与统一停重复，删掉那句"


# ------------------------------------------------------------ 页面

def test_bots页面有停止启用按钮且带二次确认():
    """源码级：页面得能停、能启回来，且停之前问一句（那一下会打断所有对话）。"""
    html = (REPO_ROOT / "moments" / "templates" / "hub_bots.html").read_text(encoding="utf-8")
    assert "/enabled'" in html or '/enabled"' in html, "页面没接停用开关端点"
    assert "confirm(" in html and "停止" in html and "启用" in html
    assert "r.body.disabled" in html, "页面没读 disabled 名单，停用的 bot 分不出灰态"
    assert "startRestart()" in html, "改完不重启 = 用户点了停止它还在回消息"
