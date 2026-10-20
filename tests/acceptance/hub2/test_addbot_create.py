# -*- coding: utf-8 -*-
"""§4.2 B/C 段 + §4.3 响应形状与那 8 条必覆盖用例。"""
import json
import re
import shutil
import threading

import pytest

import hub2util as u
from hub2util import addbot_payload, steps_by_name

EP = "/hub/api/bots"
TOKEN = "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11"


def _create(api, port, **over):
    over.setdefault("telegram_token", TOKEN)
    over.setdefault("dispatcher_port", port)
    return api.post(EP, addbot_payload(**over))


@pytest.fixture
def ok_api(apif, telegram, bot_launcher, port, restart_script):
    """一切外部条件都摆好的客户端：getMe 通、自检脚本是新版、
    重启接缝会真的拉起一个能应答 /status 的进程。"""
    telegram.mode = "ok"
    return apif(HUB_TELEGRAM_API_BASE=telegram.base,
                HUB_RESTART_CMD=bot_launcher(port),
                HUB_RESTART_SCRIPT=str(restart_script("new")))


@pytest.mark.slow
def test_加bot_全流程成功_201且五个步骤都done(ok_api, port, launchagents_dir):
    """§4.3 用例①。C 段第 7 步是探活，所以重启接缝必须真的拉起一个监听进程。"""
    code, body = _create(ok_api, port)
    assert code == 201, body
    assert body["ok"] is True
    assert body["bot_id"] == "xiaoyu"
    assert body["username"] == "my_bot"
    steps = steps_by_name(body)
    assert [s["name"] for s in body["steps"]] == ["validate", "getme", "files",
                                                  "register", "restart"]
    assert all(s["state"] == "done" for s in body["steps"]), body["steps"]
    assert steps["restart"].get("job_id")
    assert body["restart_hint"] == {"bot": True, "moments_web": True}, \
        "§13 A3：加 bot 两个都要 true —— 门户/朋友圈持有 bot 列表，不重启它新 bot 一直不可见"


@pytest.mark.slow
def test_加bot_成功后产物清单齐全且内容正确(ok_api, port,
                                            configs_dir, channels_dir):
    code, body = _create(ok_api, port, chat_id=987654321,
                         display_name="小雨", persona_text="我是小雨的人设正文。")
    assert code in (201, 207), body
    home = channels_dir / "xiaoyu"
    assert (home / ".env").read_text(encoding="utf-8").strip() == \
        f"TELEGRAM_BOT_TOKEN={TOKEN}"
    assert json.loads((home / "access.json").read_text(encoding="utf-8")) == \
        {"allowFrom": [987654321]}
    assert "我是小雨的人设正文。" in (home / "CLAUDE.md").read_text(encoding="utf-8")
    assert (home / "memory" / "MEMORY.md").exists()
    assert (home / "logs").is_dir()
    yml = u.load(configs_dir / "xiaoyu.yml")
    assert yml["id"] == "xiaoyu"
    assert yml["display_name"] == "小雨"
    assert yml["chat_id"] == 987654321
    assert yml["dispatcher_port"] == port
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                        str(yml["namespace_uuid"]))
    assert "bot_channel_path" in yml


@pytest.mark.slow
def test_加bot_成功后每个产物的权限都对(ok_api, port, configs_dir, channels_dir):
    """§4.3 用例⑧ / F5：move 跨文件系统会走 copy 路径，权限必须复核。"""
    code, body = _create(ok_api, port)
    assert code in (201, 207), body
    home = channels_dir / "xiaoyu"
    assert u.mode_of(home) == 0o700
    assert u.mode_of(home / "logs") == 0o700
    assert u.mode_of(home / ".env") == 0o600
    assert u.mode_of(home / "access.json") == 0o600
    assert u.mode_of(configs_dir / "xiaoyu.yml") == 0o600


@pytest.mark.slow
def test_加bot_token只落在env里_别处一个字都没有(ok_api, port, configs_dir,
                                                 channels_dir, capfd):
    """F5：token 的唯一落点是 channels/<bot>/.env。"""
    code, body = _create(ok_api, port)
    assert code in (201, 207), body
    out, err = capfd.readouterr()
    assert TOKEN not in u.flat_text(body), "响应里带出了 token"
    assert TOKEN not in out and TOKEN not in err, "token 进了 stdout/stderr"
    hits = []
    for root in (configs_dir, channels_dir):
        for p in root.rglob("*"):
            if p.is_file() and TOKEN in p.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(p))
    assert hits == [str(channels_dir / "xiaoyu" / ".env")], hits
    assert TOKEN not in u.text(configs_dir / "xiaoyu.yml")


def test_加bot_B段中途失败_500且磁盘干净(ok_api, port, monkeypatch, configs_dir,
                                         channels_dir, existing_bot):
    """§4.3 用例③。同时验「绝不 rm -rf 已存在目录」：既有 bot 的目录必须毫发无伤。"""
    existing_bot("chenlulu", port=17801)
    keep = channels_dir / "chenlulu"
    (keep / "keepme.txt").write_text("别删我", encoding="utf-8")

    def boom(*a, **kw):
        raise OSError("mocked move failure")

    monkeypatch.setattr(shutil, "move", boom)
    code, body = _create(ok_api, port)
    assert code == 500, body
    assert body["error"] == "internal"
    assert " " not in body["detail"].strip(), "detail 只该给异常类名"
    assert not (configs_dir / "xiaoyu.yml").exists()
    assert not (channels_dir / "xiaoyu").exists()
    assert (keep / "keepme.txt").read_text(encoding="utf-8") == "别删我"


@pytest.mark.slow
def test_加bot_注册目录只读_207且文件仍在(apif, telegram, bot_launcher, port, restart_script,
                                          tmp_path, configs_dir, channels_dir):
    """§4.3 用例④：C 段失败不回滚 B 段 —— 文件已经是对的，用户不该白填一遍表单。
    自检脚本给新版，把失败面收窄到「注册」这一步。"""
    ro = tmp_path / "readonly-agents"
    ro.mkdir()
    ro.chmod(0o500)
    telegram.mode = "ok"
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base,
               HUB_RESTART_CMD=bot_launcher(port), HUB_LAUNCHAGENTS_DIR=str(ro),
               HUB_RESTART_SCRIPT=str(restart_script("new")))
    try:
        code, body = _create(api, port)
    finally:
        ro.chmod(0o700)
    assert code == 207, body
    assert body["error"] == "bot_created_partial"
    reg = steps_by_name(body)["register"]
    assert reg["state"] == "failed"
    assert body.get("manual_hint"), "207 必须给用户能自己补的确切命令"
    assert (configs_dir / "xiaoyu.yml").exists()
    assert (channels_dir / "xiaoyu").is_dir()


@pytest.mark.slow
def test_加bot_旧版重启脚本在场_207且restart步骤是skipped(apif, telegram, bot_launcher,
                                                          port, restart_script,
                                                          configs_dir):
    """§4.3 用例⑤ / F4：老副本硬编码 BOTS=，会「正常重启了别人、退出码 0」
    —— 绝不许因此返回 201。脚本经 `HUB_RESTART_SCRIPT` 注入（§13 A2）。"""
    telegram.mode = "ok"
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base,
               HUB_RESTART_CMD=bot_launcher(port),
               HUB_RESTART_SCRIPT=str(restart_script("old")))
    code, body = _create(api, port)
    assert code == 207, body
    assert body["error"] == "bot_created_partial"
    st = steps_by_name(body)["restart"]
    assert st["state"] == "skipped"
    hint = body.get("manual_hint") or st.get("manual_hint") or ""
    assert "cp restart-bots.example.sh" in hint, f"没给两条确切命令：{hint!r}"
    assert (configs_dir / "xiaoyu.yml").exists()


@pytest.mark.slow
def test_加bot_自检脚本文件不存在_207且restart步骤是skipped(apif, telegram, bot_launcher,
                                                            port, tmp_path, configs_dir):
    """§7 `HUB_RESTART_SCRIPT` 行：**文件不存在 = 自检不通过**（`skipped` + 207）。
    重启替身照样能拉起进程，所以 207 只可能来自自检这一步。"""
    telegram.mode = "ok"
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base,
               HUB_RESTART_CMD=bot_launcher(port),
               HUB_RESTART_SCRIPT=str(tmp_path / "gone" / "restart-bots.sh"))
    code, body = _create(api, port)
    assert code == 207, body
    assert body["error"] == "bot_created_partial"
    assert steps_by_name(body)["restart"]["state"] == "skipped"
    assert (configs_dir / "xiaoyu.yml").exists()


@pytest.mark.slow
def test_加bot_脚本是新版但新bot端口探活不通_207且restart是failed(apif, telegram, port,
                                                                 restart_script,
                                                                 configs_dir):
    """§4.3 用例⑥ / F4：成功判定改探活，不看退出码。
    这里重启替身退出码 0 但什么都没起 —— 必须是 failed，不是 done。"""
    telegram.mode = "ok"
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base, HUB_RESTART_CMD="/usr/bin/true",
               HUB_RESTART_SCRIPT=str(restart_script("new")))
    code, body = _create(api, port)
    assert code == 207, body
    st = steps_by_name(body)["restart"]
    assert st["state"] == "failed", "退出码 0 就记 done = 成功响应撒谎"
    assert (configs_dir / "xiaoyu.yml").exists()


@pytest.mark.slow
def test_加bot_重复加同名bot_第二次409_bot_exists且不覆盖(ok_api, port, port2,
                                                          configs_dir, channels_dir):
    """幂等反面：同一个表单点两次，第二次必须被拒且不动第一次的产物。"""
    first = _create(ok_api, port)
    assert first[0] in (201, 207), first
    before = (configs_dir / "xiaoyu.yml").read_bytes()
    env_before = (channels_dir / "xiaoyu" / ".env").read_bytes()
    code, body = _create(ok_api, port2, bot_id="xiaoyu")
    assert code == 409, body
    assert body["error"] == "bot_exists"
    assert (configs_dir / "xiaoyu.yml").read_bytes() == before
    assert (channels_dir / "xiaoyu" / ".env").read_bytes() == env_before


@pytest.mark.concurrency
@pytest.mark.slow
def test_加bot_并发两个同端口请求_恰好一个成功一个409(apif, telegram, port,
                                                     configs_dir, channels_dir):
    """§4.3 用例⑦ / R4：两个都成功就意味着端口 TOCTOU 没堵住。"""
    telegram.mode = "ok"
    a = apif(HUB_TELEGRAM_API_BASE=telegram.base)
    clients = [a.c, a.app.test_client()]
    results = []
    gate = threading.Barrier(2)

    def go(bot_id, client):
        gate.wait()
        r = client.post(EP, json=addbot_payload(bot_id=bot_id, telegram_token=TOKEN,
                                                dispatcher_port=port))
        results.append((bot_id, r.status_code, r.get_json()))

    ts = [threading.Thread(target=go, args=("aaa_bot", clients[0])),
          threading.Thread(target=go, args=("bbb_bot", clients[1]))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=90)

    assert len(results) == 2, results
    winners = [r for r in results if r[1] in (201, 207)]
    losers = [r for r in results if r[1] == 409]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0][2]["error"] in ("bot_exists", "port_in_use", "config_busy")
    made = [b for b in ("aaa_bot", "bbb_bot") if (configs_dir / f"{b}.yml").exists()]
    assert made == [winners[0][0]], f"磁盘上产物不止一份：{made}"


@pytest.mark.slow
@pytest.mark.parametrize("bot_id", [
    pytest.param("ab", id="最短两字符"),
    pytest.param("a" + "b" * 31, id="最长32字符"),
])
def test_加bot_botid长度边界合法_创建成功(ok_api, port, configs_dir, bot_id):
    code, body = _create(ok_api, port, bot_id=bot_id)
    assert code in (201, 207), body
    assert (configs_dir / f"{bot_id}.yml").exists()


@pytest.mark.slow
def test_加bot_显示名是unicode与emoji_原样落盘(ok_api, port, configs_dir):
    name = "小雨🌧️ちゃん"
    code, body = _create(ok_api, port, display_name=name)
    assert code in (201, 207), body
    assert u.load(configs_dir / f"xiaoyu.yml")["display_name"] == name
