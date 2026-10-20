# -*- coding: utf-8 -*-
"""`moments/hub_addbot.py` 纯函数层单测（不起 HTTP，验收那层管接口形状）。

盯的是**验收用例看不见的地方**：分型表的边界格、回滚清单删得干不干净、
207 的三种分型各自来自哪一步、以及 token 在各条失败路径上会不会漏出去。
桩用 monkeypatch 替 `requests.Session`，全程不联网、不碰真 `~/.claude/`。
"""
import json
import os

import pytest

from moments import hub_addbot as ab
from moments.provider_model import HubError

TOKEN = "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11"


@pytest.fixture(autouse=True)
def _seams(tmp_path, monkeypatch):
    """五个接缝全钉在 tmp：落盘、模板扫描、`.env` 比对、plist、自检脚本都不许出 tmp。"""
    for name in ("configs", "channels", "agents"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(tmp_path / "channels"))
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("HUB_RESTART_SCRIPT", str(tmp_path / "no-such.sh"))
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", "http://127.0.0.1:9")
    return tmp_path


def fields(**over):
    body = {"bot_id": "xiaoyu", "telegram_token": TOKEN, "dispatcher_port": 17899,
            "chat_id": 123456789, "display_name": "小雨", "persona_text": "正文"}
    body.update(over)
    return body


class _Resp:
    def __init__(self, status, raw):
        self.status_code, self._raw = status, raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, size):
        for i in range(0, len(self._raw), size):
            yield self._raw[i:i + size]


@pytest.fixture
def stub_get(monkeypatch):
    """替掉 requests.Session：单测全程不发一个包，也不依赖桩服务器起没起来。

    返回的 `calls` 列表同时是"到底发没发请求"的证据（分型表里两格要求**一个包都不发**）。
    """
    calls = []

    def _install(status=200, body=None, raw=None, exc=None):
        class _Session:
            trust_env = True

            def get(self, url, **kw):
                calls.append((url, kw))
                if exc is not None:
                    raise exc
                blob = raw if raw is not None else json.dumps(body or {}).encode()
                return _Resp(status, blob)

            def close(self):
                pass

        monkeypatch.setattr(ab.requests, "Session", _Session)
        return calls

    _install.calls = calls
    return _install


# ---------------------------------------------------------------- getMe 分型（§4.1）

def test_基址形状非法一律抛_且不回显原文(monkeypatch):
    for bad in ("not-a-url", "https://api.telegram.org/?x=1", "https://a.example/bot",
                "ftp://a.example", ""):
        monkeypatch.setenv("HUB_TELEGRAM_API_BASE", bad or "not-a-url")
        with pytest.raises(RuntimeError) as e:
            ab.api_base()
        assert bad not in str(e.value) or bad == ""


def test_基址未设时走真实域名(monkeypatch):
    monkeypatch.delenv("HUB_TELEGRAM_API_BASE", raising=False)
    assert ab.api_base() == "https://api.telegram.org"


def test_基址尾斜杠被剥掉_不会拼出双斜杠(monkeypatch):
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", "https://a.example/")
    assert ab.api_base() == "https://a.example"


@pytest.mark.parametrize("bad", ["", "   ", "12345:short", "123456789", None, 123,
                                 "abcdefghi:AAaaBBbbCCccDDddEEeeFFggHHhh11"])
def test_token形状预检不过_malformed且一个包都不发(stub_get, bad):
    calls = stub_get()
    assert ab.getme(bad) == {"ok": False, "stage": "malformed"}
    assert calls == []


def test_明文http非回环_blocked_base且一个包都不发(monkeypatch, stub_get):
    calls = stub_get()
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", "http://evil.example")
    out = ab.getme(TOKEN)
    assert out == {"ok": False, "stage": "blocked_base"}
    assert calls == [], "拒发却发了包"


def test_明文http回环放行_桩绑127不受影响(monkeypatch, stub_get):
    calls = stub_get(body={"ok": True, "result": {"id": 7, "username": "u"}})
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", "http://localhost:8081")
    assert ab.getme(TOKEN)["ok"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("status,body,stage,code", [
    (401, {"error_code": 401, "description": "Unauthorized"}, "invalid_token", 401),
    (404, {"error_code": 404}, "invalid_token", 404),
    (429, {"error_code": 429, "description": "Too Many Requests"}, "rejected", 429),
    (400, {"error_code": 400}, "rejected", 400),
    (500, {"error_code": 500}, "upstream", 500),
    (502, {}, "upstream", 502),
    (200, {"ok": False, "error_code": 429}, "rejected", 429),      # R9-1
])
def test_getMe分型表逐行(stub_get, status, body, stage, code):
    stub_get(status=status, body=body)
    out = ab.getme(TOKEN)
    assert out["ok"] is False and out["stage"] == stage and out["error_code"] == code


@pytest.mark.parametrize("kw", [
    {"status": 302, "body": {"ok": True, "result": {"username": "x", "id": 1}}},   # R9-3
    {"status": 200, "body": {"ok": True, "result": {"id": 1}}},                    # R9-2
    {"status": 200, "body": {"ok": True, "result": "不是对象"}},
    {"status": 200, "raw": b"<html>not json</html>"},
    {"status": 200, "raw": b'{"ok": true, "result": {"pad": "' + b"P" * (70 * 1024)},  # R2-2
])
def test_坏响应一律bad_response(stub_get, kw):
    stub_get(**kw)
    assert ab.getme(TOKEN) == {"ok": False, "stage": "bad_response"}


def test_超时与连不上分开报_detail只给异常类名(stub_get):
    stub_get(exc=ab.requests.exceptions.ConnectTimeout("boom"))
    assert ab.getme(TOKEN) == {"ok": False, "stage": "timeout"}
    stub_get(exc=ab.requests.exceptions.ConnectionError("connect to 1.2.3.4 failed"))
    out = ab.getme(TOKEN)
    assert out["stage"] == "network" and out["detail"] == "ConnectionError"
    assert " " not in out["detail"]


LEAK = "987654321:AAFleakLeakLeakLeakLeakLeakLeak0000"


def test_上游回显token与请求URL_一个字都不带出来(stub_get):
    """S6：Telegram 的 URL 里就有 token，`description` 原样转发就是泄漏。
    抹成 `***` 还不够 —— 剩下的 `/bot***/getMe` 仍是「原始请求 URL」。"""
    stub_get(status=401, body={"error_code": 401,
                               "description": "bad token=%s url=/bot%s/getMe" % (LEAK, LEAK)})
    blob = json.dumps(ab.getme(TOKEN), ensure_ascii=False)
    assert LEAK not in blob and "/bot" not in blob


def test_描述里的控制字符被剔除且截到200字(stub_get):
    stub_get(status=429, body={"error_code": 429,
                               "description": "\x00\x1b[31m危险\x07" + "A" * 500})
    desc = ab.getme(TOKEN)["description"]
    assert len(desc) <= 200 and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in desc)


def test_成功时只带打码token(stub_get):
    stub_get(body={"ok": True, "result": {"id": 123456789, "username": "my_bot"}})
    out = ab.test_token(TOKEN)
    assert out == {"ok": True, "username": "my_bot", "bot_id": 123456789,
                   "token_masked": "****hh11"}
    assert TOKEN not in json.dumps(out)


def test_失败时不附打码token(stub_get):
    stub_get(status=401, body={"error_code": 401})
    assert "token_masked" not in ab.test_token(TOKEN)


# ---------------------------------------------------------------- A 段（§4.2）

@pytest.mark.parametrize("field", ["bot_id", "telegram_token", "dispatcher_port", "chat_id"])
@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_必填字段缺失或空白_bad_body且detail点名(field, value):
    body = fields()
    if value is None:
        body.pop(field)
    else:
        body[field] = value
    with pytest.raises(HubError) as e:
        ab._validate(body)
    assert (e.value.status, e.value.error) == (400, "bad_body")
    assert field in e.value.detail


@pytest.mark.parametrize("bot_id", ["Xiaoyu", "_xiaoyu", "1xiaoyu", "a", "a" * 33,
                                    "小雨", "xiao-yu", "xiao yu", "xiaoyu/../etc", 123])
def test_botid不合形状_bad_field(bot_id):
    with pytest.raises(HubError) as e:
        ab._validate(fields(bot_id=bot_id))
    assert e.value.error == "bad_field"


@pytest.mark.parametrize("bot_id", ["ab", "a" + "b" * 31, "x_9"])
def test_botid长度边界合法(bot_id):
    assert ab._validate(fields(bot_id=bot_id))["bot_id"] == bot_id


@pytest.mark.parametrize("port", [1023, 65536, 0, -1, "17802", 17802.0, None, True])
def test_端口不合形状_bad_field(port):
    with pytest.raises(HubError) as e:
        ab._validate(fields(dispatcher_port=port))
    assert e.value.error == "bad_field", port


@pytest.mark.parametrize("chat_id", ["123", 12.5, None, [1], True])
def test_chatid非整数_bad_field(chat_id):
    with pytest.raises(HubError) as e:
        ab._validate(fields(chat_id=chat_id))
    assert e.value.error == "bad_field"


def test_人设二选一_都给或都不给都是bad_field(_seams):
    (_seams / "channels" / "tmpl").mkdir()
    for over in ({"persona_text": "正文", "persona_template": "tmpl"},
                 {"persona_text": None, "persona_template": None}):
        with pytest.raises(HubError) as e:
            ab._validate(fields(**over))
        assert e.value.error == "bad_field"


def test_人设模板走目录白名单_穿越串落不进去(_seams):
    (_seams / "channels" / "tmpl").mkdir()
    (_seams / "channels" / "tmpl" / "CLAUDE.md").write_text("模板正文", encoding="utf-8")
    got = ab._validate(fields(persona_text=None, persona_template="tmpl"))
    assert got["persona"] == "模板正文"
    for bad in ("../../etc/passwd", "tmpl/..", "没这个"):
        with pytest.raises(HubError) as e:
            ab._validate(fields(persona_text=None, persona_template=bad))
        assert e.value.error == "bad_field"


def test_人设正文超64KiB_413(_seams):
    with pytest.raises(HubError) as e:
        ab._validate(fields(persona_text="人" * (64 * 1024)))
    assert (e.value.status, e.value.error) == (413, "payload_too_large")


@pytest.mark.parametrize("name", [None, 123, "名" * 65, "小\x00雨", "小\n雨", "   "])
def test_显示名非法_bad_field(name):
    with pytest.raises(HubError) as e:
        ab._validate(fields(display_name=name))
    assert e.value.error in ("bad_field", "bad_body")


def test_模板列表_目录读不到时空列表不是错误(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(tmp_path / "gone"))
    assert ab.templates() == []


def _existing_bot(root, bot_id="laoda", port=17801, token_head="123456789"):
    (root / "configs" / (bot_id + ".yml")).write_text(
        "id: %s\ndisplay_name: 老大\ndispatcher_port: %d\n"
        "namespace_uuid: 11111111-2222-3333-4444-555555555555\n" % (bot_id, port),
        encoding="utf-8")
    d = root / "channels" / bot_id
    d.mkdir(exist_ok=True)
    (d / ".env").write_text("TELEGRAM_BOT_TOKEN=%s:xxxx\n" % token_head, encoding="utf-8")
    return d


def test_同名判定看两处_只建了频道目录也算占用(_seams):
    (_seams / "channels" / "xiaoyu").mkdir()
    with pytest.raises(HubError) as e:
        ab._validate(fields())
    assert (e.value.status, e.value.error) == (409, "bot_exists")


def test_端口被注册表里别的bot占_bot_exists且点名占用者(_seams):
    _existing_bot(_seams, port=17899)
    with pytest.raises(HubError) as e:
        ab._validate(fields(dispatcher_port=17899))
    assert (e.value.status, e.value.error) == (409, "bot_exists")
    assert "laoda" in e.value.detail


def test_端口被本机进程监听_port_in_use而不是bot_exists(_seams):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    try:
        with pytest.raises(HubError) as e:
            ab._validate(fields(dispatcher_port=s.getsockname()[1]))
    finally:
        s.close()
    assert (e.value.status, e.value.error) == (409, "port_in_use")


def test_token已被别的bot用_token_in_use且比对只吃数字段(_seams):
    _existing_bot(_seams, token_head="123456789")
    with pytest.raises(HubError) as e:
        ab._check_token_free("xiaoyu", 123456789)
    assert (e.value.status, e.value.error) == (409, "token_in_use")
    ab._check_token_free("xiaoyu", 987654321)          # 别人的 id，不该拦
    ab._check_token_free("laoda", 123456789)           # 就是它自己，不该拦


def test_env缺失或形状不符的bot被跳过_不报错(_seams):
    _existing_bot(_seams, bot_id="a1")
    (_seams / "channels" / "a1" / ".env").write_text("NOISE=1\n", encoding="utf-8")
    _existing_bot(_seams, bot_id="a2", port=17888)
    os.unlink(str(_seams / "channels" / "a2" / ".env"))
    assert ab._telegram_ids() == {}


# ---------------------------------------------------------------- B 段 · 落盘与回滚

import stat as _stat


def _mode(p):
    return _stat.S_IMODE(os.stat(str(p)).st_mode)


def test_落盘产物清单齐全_权限对_token只在env里(_seams):
    ab.lay_down(ab._validate(fields()))
    home = _seams / "channels" / "xiaoyu"
    yml = _seams / "configs" / "xiaoyu.yml"
    assert (home / ".env").read_text(encoding="utf-8").strip() == "TELEGRAM_BOT_TOKEN=" + TOKEN
    assert json.loads((home / "access.json").read_text(encoding="utf-8")) == \
        {"allowFrom": [123456789]}
    assert (home / "CLAUDE.md").read_text(encoding="utf-8") == "正文"
    assert (home / "memory" / "MEMORY.md").exists() and (home / "logs").is_dir()
    assert (_mode(home), _mode(home / "logs"), _mode(home / "memory")) == (0o700,) * 3
    assert (_mode(home / ".env"), _mode(home / "access.json"), _mode(yml)) == (0o600,) * 3
    hits = [str(p) for p in _seams.rglob("*")
            if p.is_file() and TOKEN in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == [str(home / ".env")], hits


def test_落盘的yml不含token且字段齐全(_seams):
    import yaml
    ab.lay_down(ab._validate(fields(display_name="小雨🌧️ちゃん")))
    text = (_seams / "configs" / "xiaoyu.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert TOKEN not in text
    assert data["id"] == "xiaoyu" and data["display_name"] == "小雨🌧️ちゃん"
    assert data["chat_id"] == 123456789 and data["dispatcher_port"] == 17899
    assert len(data["namespace_uuid"]) == 36 and "bot_channel_path" in data


def test_namespace每次都新_且不与既有表撞(_seams):
    seen = set()
    for i in range(3):
        ab.lay_down(ab._validate(fields(bot_id="bot_%d" % i, dispatcher_port=17900 + i)))
        import yaml
        seen.add(yaml.safe_load(
            (_seams / "configs" / ("bot_%d.yml" % i)).read_text(encoding="utf-8"))["namespace_uuid"])
    assert len(seen) == 3


def test_move失败_回滚后磁盘与操作前一致(_seams, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        ab.lay_down(ab._validate(fields()))
    assert not (_seams / "channels" / "xiaoyu").exists()
    assert not (_seams / "configs" / "xiaoyu.yml").exists()
    assert list((_seams / "channels").iterdir()) == []


def test_写yml失败_已就位的频道树被精确删掉(_seams, monkeypatch):
    real = ab._create_file

    def boom(path, text, mode=0o600):
        if path.endswith(".yml"):
            raise OSError("disk full")
        return real(path, text, mode)

    monkeypatch.setattr(ab, "_create_file", boom)
    with pytest.raises(OSError):
        ab.lay_down(ab._validate(fields()))
    assert not (_seams / "channels" / "xiaoyu").exists()
    assert not (_seams / "configs" / "xiaoyu.yml").exists()


def test_回滚绝不碰目录里别人的东西(_seams, monkeypatch):
    """`rm -rf` 版的回滚会把这个文件一起删掉 —— 那是别人的聊天记录。"""
    keep = _existing_bot(_seams, bot_id="laoda", port=17801)
    (keep / "keepme.txt").write_text("别删我", encoding="utf-8")
    ab._rollback([(True, str(keep)), (False, str(_seams / "configs" / "laoda.yml"))])
    assert (keep / "keepme.txt").read_text(encoding="utf-8") == "别删我"
    assert keep.is_dir(), "非空目录被删了"


def test_写完后端口被别人抢走_复查拦下并回滚(_seams, monkeypatch):
    real = ab._check_port

    def late(port, skip=None):
        if skip is not None:                       # 只让"写完那次"复查失败
            raise HubError(409, "bot_exists", "端口 %d 已被 laoda 占用" % port)
        return real(port, skip)

    monkeypatch.setattr(ab, "_check_port", late)
    with pytest.raises(HubError) as e:
        ab.lay_down(ab._validate(fields()))
    assert e.value.error == "bot_exists"
    assert not (_seams / "channels" / "xiaoyu").exists()
    assert not (_seams / "configs" / "xiaoyu.yml").exists()


# ---------------------------------------------------------------- C 段 · 207 三种分型

class _Jobs:
    """假 job 表：只记有没有被叫到，绝不真起进程。"""

    def __init__(self, job_id="job1234"):
        self.started = []
        self._id = job_id

    def start(self, argv):
        self.started.append(argv)
        return type("Acc", (), {"job_id": self._id, "started_at": 0, "busy": None})()


def _new_script(root):
    p = root / "restart-new.sh"
    p.write_text('out="$(python3 -m bots_registry --format=sh)" || exit 1\n', encoding="utf-8")
    return str(p)


def _old_script(root):
    p = root / "restart-old.sh"
    p.write_text('BOTS=("laoda:17801")\n', encoding="utf-8")
    return str(p)


def test_自检_新版通过_旧版与文件不存在都不通过(_seams, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_SCRIPT", _new_script(_seams))
    assert ab._script_reads_registry() is True
    monkeypatch.setenv("HUB_RESTART_SCRIPT", _old_script(_seams))
    assert ab._script_reads_registry() is False
    monkeypatch.setenv("HUB_RESTART_SCRIPT", str(_seams / "gone.sh"))
    assert ab._script_reads_registry() is False


def test_旧版脚本_restart是skipped且给两条确切命令(_seams, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_SCRIPT", _old_script(_seams))
    jobs = _Jobs()
    step = ab.restart(ab._validate(fields()), jobs)
    assert step["state"] == "skipped" and jobs.started == [], "自检没过却还是把重启发出去了"
    assert "cp restart-bots.example.sh" in step["manual_hint"]


def test_脚本是新版但探活不通_是failed不是done(_seams, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_SCRIPT", _new_script(_seams))
    monkeypatch.setenv("HUB_RESTART_CMD", "/usr/bin/true")
    monkeypatch.setattr(ab, "wait_alive", lambda port, **k: False)
    step = ab.restart(ab._validate(fields()), _Jobs())
    assert step["state"] == "failed" and step["job_id"] == "job1234"
    assert step["manual_hint"]


def test_探活通了才记done(_seams, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_SCRIPT", _new_script(_seams))
    monkeypatch.setenv("HUB_RESTART_CMD", "/usr/bin/true")
    monkeypatch.setattr(ab, "wait_alive", lambda port, **k: True)
    assert ab.restart(ab._validate(fields()), _Jobs())["state"] == "done"


def test_注册目录只读_register是failed且给能自己补的命令(_seams):
    if os.name == "nt" or os.getuid() == 0:
        pytest.skip("root 或 Windows 上只读目录拦不住写入")
    ro = _seams / "agents"
    ro.chmod(0o500)
    try:
        step = ab.register(ab._validate(fields()))
    finally:
        ro.chmod(0o700)
    assert step["state"] in ("failed", "skipped")
    assert step["manual_hint"], "207 不给命令 = 用户不知道差哪一步"


def test_接缝非默认时不实调launchctl_只记skipped_launchctl(_seams, monkeypatch):
    import sys as _sys
    if _sys.platform != "darwin":
        pytest.skip("非 macOS 没有 launchd 分支")
    called = []
    monkeypatch.setattr(ab.subprocess, "run",
                        lambda *a, **k: called.append(a) or type("R", (), {"returncode": 0})())
    step = ab.register(ab._validate(fields()))
    assert step == {"name": "register", "state": "done", "detail": "skipped_launchctl"}
    plist = _seams / "agents" / "com.example.xiaoyu-self-initiate.plist"
    text = plist.read_text(encoding="utf-8")
    assert "xiaoyu" in text and "123456789" in text and "{BOT_ID}" not in text
    # 只比 argv[0]：tmp_path 的目录名里就带着本用例的名字（含 launchctl 三个字），
    # 拿 str(整个调用) 去匹配会永远为真
    assert not any(a[0][0].endswith("launchctl") for a in called), "接缝被注入了却动了 gui domain"
    assert list((_seams / "agents").glob("*.tmp")) == [], "留下了临时文件"


# ---------------------------------------------------------------- 三段总装（§4.3）

def test_getMe失败_200且ok为false且磁盘一个产物都没有(_seams, stub_get):
    stub_get(status=401, body={"error_code": 401, "description": "Unauthorized"})
    body, code = ab.create(fields(), _Jobs())
    assert (code, body["ok"], body["stage"]) == (200, False, "invalid_token")
    assert list((_seams / "channels").iterdir()) == []
    assert list((_seams / "configs").glob("*.yml")) == []


def test_全成功_201且五步都done_restart_hint两个都真(_seams, stub_get, monkeypatch):
    stub_get(body={"ok": True, "result": {"id": 123456789, "username": "my_bot"}})
    monkeypatch.setenv("HUB_RESTART_SCRIPT", _new_script(_seams))
    monkeypatch.setenv("HUB_RESTART_CMD", "/usr/bin/true")
    monkeypatch.setattr(ab, "wait_alive", lambda port, **k: True)
    body, code = ab.create(fields(), _Jobs())
    assert code == 201 and body["ok"] is True and body["username"] == "my_bot"
    assert [s["name"] for s in body["steps"]] == ["validate", "getme", "files",
                                                  "register", "restart"]
    assert all(s["state"] == "done" for s in body["steps"]), body["steps"]
    assert body["restart_hint"] == {"bot": True, "moments_web": True}
    assert "manual_hint" not in body and "error" not in body


def test_C段没走完_207且文件仍在且必给manual_hint(_seams, stub_get):
    """自检脚本不存在（夹具默认）→ restart 是 skipped → 整体 207，但 B 段产物必须留着：
    文件已经是对的，删掉等于用户白填一遍表单。"""
    stub_get(body={"ok": True, "result": {"id": 123456789, "username": "my_bot"}})
    body, code = ab.create(fields(), _Jobs())
    assert code == 207 and body["error"] == "bot_created_partial" and body["ok"] is False
    assert {s["name"]: s["state"] for s in body["steps"]}["restart"] == "skipped"
    assert body["manual_hint"]
    assert (_seams / "configs" / "xiaoyu.yml").exists()
    assert (_seams / "channels" / "xiaoyu").is_dir()


def test_token已被别的bot用_落盘前就拦住(_seams, stub_get):
    _existing_bot(_seams, token_head="123456789")
    stub_get(body={"ok": True, "result": {"id": 123456789, "username": "my_bot"}})
    with pytest.raises(HubError) as e:
        ab.create(fields(), _Jobs())
    assert (e.value.status, e.value.error) == (409, "token_in_use")
    assert not (_seams / "channels" / "xiaoyu").exists()


def test_B段异常翻成500_internal且detail只有异常类名(_seams, stub_get, monkeypatch):
    stub_get(body={"ok": True, "result": {"id": 1, "username": "my_bot"}})
    monkeypatch.setattr(ab, "lay_down", lambda f: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(HubError) as e:
        ab.create(fields(), _Jobs())
    assert (e.value.status, e.value.error, e.value.detail) == (500, "internal", "OSError")
    assert " " not in e.value.detail


def test_全流程任何一条失败路径都不把token带进响应(_seams, stub_get, monkeypatch):
    """token 只该在 `.env` 里。这条把三条失败路径的响应体拍平了一起看。"""
    blobs = []
    stub_get(status=401, body={"error_code": 401, "description": "token=" + TOKEN})
    blobs.append(json.dumps(ab.create(fields(), _Jobs())[0], ensure_ascii=False))
    stub_get(body={"ok": True, "result": {"id": 1, "username": "my_bot"}})
    blobs.append(json.dumps(ab.create(fields(), _Jobs())[0], ensure_ascii=False))   # 207
    monkeypatch.setattr(ab, "lay_down", lambda f: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(HubError) as e:
        ab.create(fields(bot_id="another"), _Jobs())
    blobs.append(e.value.detail)
    for blob in blobs:
        assert TOKEN not in blob and "/bot" not in blob


def test_并发创建_第二个立刻409_config_busy():
    """锁是非阻塞的：第二个请求当场回 409，不会挂在那儿等 30 秒探活。"""
    from flask import Flask
    app = Flask(__name__)
    with app.app_context():
        with ab.hold_addbot_lock():
            with pytest.raises(HubError) as e:
                with ab.hold_addbot_lock():
                    pass
        assert (e.value.status, e.value.error) == (409, "config_busy")
        with ab.hold_addbot_lock():          # 上一次已释放，再拿得到
            pass
