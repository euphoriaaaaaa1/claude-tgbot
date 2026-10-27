# -*- coding: utf-8 -*-
"""provider-hub 二期验收夹具（黑盒）。

只依据 `.devflow/BRIEF-hub2.md` 与 `INTERFACE-hub2.md` 编写，不读实现代码。
实现未落地时本目录整体红 —— 那是红基线，不是夹具坏了。

硬隔离（§7 测试接缝 + BRIEF 敏感面）：
1. `HUB_CONFIGS_DIR` 指 tmp；autouse 二次断言它不等于仓库 `configs/`。
2. `HUB_TELEGRAM_API_BASE` autouse 强制指向 127.0.0.1（未设或指向真实域名 → 该用例直接 fail），
   任何用例都不可能打到 api.telegram.org。
3. `HUB_LAUNCHAGENTS_DIR` 指 tmp；`CLAUDE_SETTINGS_PATH` / `HUB_ENV_FILE` 沿一期做法钉在 tmp。
4. **`HUB_CHANNELS_DIR` 指 tmp**（§13 A2 新增接缝）：频道根、模板扫描、A7 的 `.env` 比对全经它。
   `HOME` 仍钉在 tmp，但**它不再是接缝、只是防炸垫**：实现若无视 `HUB_CHANNELS_DIR`，
   产物会落进 fakehome 而不是用户真实 `~/.claude/channels/`（那里跑着生产 bot），
   用例照样红在「产物不在 `HUB_CHANNELS_DIR` 下」这条断言上。
5. **`HUB_RESTART_SCRIPT` 默认指向一个不存在的 tmp 路径**（§13 A2 新增接缝）：
   C 段第 5 步自检恒不通过 → 不关心重启的用例结果确定（207），且绝不去读仓库根/用户的工作副本。
   需要具体脚本的用例用 `restart_script` 夹具造一份，再经 `apif(HUB_RESTART_SCRIPT=...)` 注入。
6. 不监听生产端口、不起真 bot、不调 launchctl；假 token 形如 `123456789:test-...`。
"""
import importlib
import os
import socket
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = _ROOT
REAL_HOME = Path(os.path.expanduser("~"))          # 在 monkeypatch HOME 之前定住
REAL_CONFIGS = REPO_ROOT / "configs"
REAL_CHANNELS = REAL_HOME / ".claude" / "channels"
REAL_SETTINGS = REAL_HOME / ".claude" / "settings.json"

FAKE_TOKEN = "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11"
FAKE_TOKEN_2 = "123456780:test-ZZzzYYyyXXxxWWwwVVvvUUuuTTss22"

import spec  # noqa: E402
from stub_telegram import StubTelegram  # noqa: E402


def _load_app():
    """重新导入门户 Flask app（env 在导入期被读，改 env 必须重载）。照抄一期做法。"""
    mod = sys.modules.get("moments.web")
    mod = importlib.reload(mod) if mod else importlib.import_module("moments.web")
    for attr in ("app", "application"):
        obj = getattr(mod, attr, None)
        if obj is not None and hasattr(obj, "test_client"):
            return obj
    factory = getattr(mod, "create_app", None)
    if callable(factory):
        return factory()
    raise AssertionError("moments.web 未导出可用的 Flask app（试过 app/application/create_app()）")


def free_port():
    """拿一个当前空闲的端口号并立刻释放（用于"没人监听"的前提）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _snapshot(d):
    d = Path(d)
    if not d.exists():
        return None
    out = {}
    for p in sorted(d.iterdir()):
        try:
            out[p.name] = (p.is_dir(), p.stat().st_mtime_ns, p.stat().st_size)
        except OSError:
            out[p.name] = ("gone",)
    return out


@pytest.fixture
def configs_dir(tmp_path):
    """§7 `HUB_CONFIGS_DIR`：`_global.yml` / `<bot>.yml` / 备份全在其下。"""
    d = tmp_path / "configs"
    d.mkdir()
    assert d.resolve() != REAL_CONFIGS.resolve(), "HUB_CONFIGS_DIR 指到了真实 configs/"
    return d


@pytest.fixture
def global_yml(configs_dir):
    """写好样本 `_global.yml`（含注释/锚点/merge key/块标量/敏感 key），返回路径。"""
    p = configs_dir / "_global.yml"
    p.write_text(spec.global_yml(), encoding="utf-8")
    os.chmod(p, 0o600)
    return p


@pytest.fixture
def launchagents_dir(tmp_path):
    """§7 `HUB_LAUNCHAGENTS_DIR`：设为非默认值时不许实调 launchctl。"""
    d = tmp_path / "LaunchAgents"
    d.mkdir()
    return d


@pytest.fixture
def telegram():
    """§7 冻结的 getMe 桩，绑 127.0.0.1 随机端口。"""
    s = StubTelegram().start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, configs_dir, launchagents_dir, channels_dir):
    """autouse：任何用例都不许继承外部真实口令/家目录/配置目录。"""
    for k in ("HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "HUB_RESTART_CMD",
              "MOMENTS_WEB_PORT", "ACCESS_PASSWORD", "CLIPROXY_MGMT_KEY"):
        monkeypatch.delenv(k, raising=False)
    fake_home = tmp_path / "fakehome"
    (fake_home / ".claude" / "channels").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))   # 防炸垫，不是接缝（见模块 docstring 4）
    monkeypatch.setenv("CLAUDE_TGBOT_HOME", str(tmp_path / "tgbot-home"))
    (tmp_path / "tgbot-home").mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(fake_home / ".claude" / "settings.json"))
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))
    monkeypatch.setenv("MOMENTS_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs_dir))
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(launchagents_dir))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(channels_dir))          # §13 A2
    # §13 A2：默认指向不存在的文件 = C 段自检必不通过；要具体脚本的用例自己注入
    monkeypatch.setenv("HUB_RESTART_SCRIPT", str(tmp_path / "no-such-restart-bots.sh"))
    # §7：未设 base 即 fail 该用例。默认指一个没人监听的回环端口（= network 分型）。
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", f"http://127.0.0.1:{free_port()}")
    base = os.environ.get("HUB_TELEGRAM_API_BASE", "")
    assert base, "HUB_TELEGRAM_API_BASE 未设 → 直接判该用例失败（§7）"
    assert "127.0.0.1" in base or "localhost" in base, f"getMe base 不是回环：{base}"


def _names(d):
    """只取顶层条目名（+是否目录）。用于本机跑着生产 bot 的目录：
    它们的 mtime 会被真 bot 自己的写入不断刷新，拿 mtime 比对必然假红。
    接缝漏了的特征是**冒出/消失一个条目**（如 `channels/xiaoyu/`），名字集合就能抓住。"""
    d = Path(d)
    if not d.exists():
        return None
    return {p.name: p.is_dir() for p in d.iterdir()}


@pytest.fixture(autouse=True)
def _no_touch_real():
    """反向断言：跑完任何用例后，真实 configs/、真实 ~/.claude/channels、
    真实 settings.json、真实 LaunchAgents 必须一字未动（接缝漏了就在这里炸）。"""
    real_la = REAL_HOME / "Library" / "LaunchAgents"
    before = (_snapshot(REAL_CONFIGS), _names(REAL_CHANNELS), _snapshot(real_la),
              REAL_SETTINGS.stat().st_mtime_ns if REAL_SETTINGS.exists() else None)
    yield
    after = (_snapshot(REAL_CONFIGS), _names(REAL_CHANNELS), _snapshot(real_la),
             REAL_SETTINGS.stat().st_mtime_ns if REAL_SETTINGS.exists() else None)
    assert before[0] == after[0], "用例改动了仓库真实 configs/"
    assert before[1] == after[1], "用例在真实 ~/.claude/channels/ 下增删了条目"
    assert before[2] == after[2], "用例改动了真实 ~/Library/LaunchAgents/"
    assert before[3] == after[3], "用例改动了真实 ~/.claude/settings.json"


@pytest.fixture
def make_client(monkeypatch, configs_dir, launchagents_dir):
    """(**env) -> (client, app)。按给定 env 重载门户拿测试客户端。"""
    def _make(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))
        app = _load_app()
        app.config["TESTING"] = True
        return app.test_client(), app
    return _make


class _Api:
    """(code, body) 小工具；body 解不出 JSON 时给 {"_raw_text", "_ctype"}。"""

    def __init__(self, client):
        self.c = client

    @staticmethod
    def _wrap(r):
        try:
            j = r.get_json()
        except Exception:
            j = None
        if j is None:
            return r.status_code, {"_raw_text": r.get_data(as_text=True)[:2000],
                                   "_ctype": r.headers.get("Content-Type", "")}
        return r.status_code, j

    def get(self, path, **kw):
        return self._wrap(self.c.get(path, **kw))

    def post(self, path, payload=None, **kw):
        if payload is not None and "data" not in kw:
            kw["json"] = payload
        elif payload is None and "data" not in kw and "json" not in kw:
            kw["json"] = {}
        return self._wrap(self.c.post(path, **kw))

    def raw_get(self, path, **kw):
        return self.c.get(path, **kw)


@pytest.fixture
def client(make_client):
    """默认客户端：门关闭（一期老用户形态），可直达 /hub*。"""
    c, _ = make_client()
    return c


@pytest.fixture
def api(client):
    return _Api(client)


@pytest.fixture
def apif(make_client):
    """需要自定义 env 时用：apif(HUB_TELEGRAM_API_BASE=...) -> _Api。"""
    def _f(**env):
        c, app = make_client(**env)
        a = _Api(c)
        a.app = app          # 并发用例要从同一个 app 再开一个 client（共享进程内锁）
        return a
    return _f


@pytest.fixture(autouse=True)
def _safe_restart_cmd(monkeypatch, _isolate):
    """默认把重启接缝钉在 no-op 上 —— 不设就会去跑真的 restart-bots.sh。"""
    monkeypatch.setenv("HUB_RESTART_CMD", "/usr/bin/true")


_MINI_BOT = '''\
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        raw = json.dumps({"phase": "ready", "pid": 4242, "queue_depth": 0}).encode()
        self.send_response(200 if self.path.split("?")[0] == "/status" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        self.send_response(405); self.send_header("Content-Length", "0"); self.end_headers()
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


@pytest.fixture
def bot_launcher(tmp_path):
    """(port) -> 一条 `HUB_RESTART_CMD` 用的命令：跑完立刻退 0，后台留下一个
    在该端口应答 `POST /status` 的迷你 bot —— 用来喂 §4.2 C 段第 7 步的探活。
    端口写死在脚本里，所以不依赖"门户怎么给重启命令传参"这个 INTERFACE 没写的细节。"""
    import signal
    srv = tmp_path / "mini_bot.py"
    srv.write_text(_MINI_BOT, encoding="utf-8")
    pidfile = tmp_path / "mini_bot.pids"
    spawned = []

    def _cmd(port):
        launcher = tmp_path / f"launch_{port}.py"
        launcher.write_text(
            "import subprocess, sys\n"
            f"p = subprocess.Popen([sys.executable, {str(srv)!r}, {str(port)!r}],\n"
            "                     start_new_session=True)\n"
            f"open({str(pidfile)!r}, 'a').write(str(p.pid) + '\\n')\n",
            encoding="utf-8")
        return f"{sys.executable} {launcher}"

    yield _cmd
    if pidfile.exists():
        for line in pidfile.read_text().split():
            try:
                os.kill(int(line), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    for p in spawned:
        p.kill()


@pytest.fixture
def held_port():
    """占住一个端口不放，用于 §4.2「该端口本机已被监听 → 409 port_in_use」。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    port = s.getsockname()[1]
    try:
        yield port
    finally:
        s.close()


def version_of(path):
    """§2.1 定义：`f"{st_mtime_ns}-{st_size}"`；文件不存在时 `"absent"`。"""
    p = Path(path)
    if not p.exists():
        return "absent"
    st = p.stat()
    return f"{st.st_mtime_ns}-{st.st_size}"


def login(client, password=None, admin_password=None):
    form = {}
    if password is not None:
        form["password"] = password
    if admin_password is not None:
        form["admin_password"] = admin_password
    return client.post("/login", data=form,
                       content_type="application/x-www-form-urlencoded")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 含真实等待（10s getMe 超时 / 30s 探活）")
    config.addinivalue_line("markers", "concurrency: 起线程，可能受调度影响")


@pytest.fixture
def as_api():
    """把任意 test_client 包成 (code, body) 形式（避免用例去 import conftest）。"""
    return _Api


@pytest.fixture
def do_login():
    return login


@pytest.fixture
def channels_dir(tmp_path):
    """§7 `HUB_CHANNELS_DIR`（§13 A2 新增接缝）：频道根。
    B 段产物树、§4.4 模板扫描、A7 的 `.env` 比对都必须落在这里 —— 落到别处即红。"""
    d = tmp_path / "channels"
    d.mkdir(parents=True, exist_ok=True)
    assert d.resolve() != REAL_CHANNELS.resolve(), "HUB_CHANNELS_DIR 指到了真实 channels/"
    return d


@pytest.fixture
def persona_template(channels_dir):
    """造一个可选的人设模板：`channels/<id>/CLAUDE.md`（§4.2 的可选值来源）。"""
    d = channels_dir / "chenlulu"
    d.mkdir(parents=True, exist_ok=True)
    (d / "CLAUDE.md").write_text("我是陈璐璐模板正文。\n", encoding="utf-8")
    return "chenlulu"


@pytest.fixture
def existing_bot(configs_dir, channels_dir):
    """预置一个"已存在的 bot"：configs/<id>.yml + channels/<id>/。"""
    def _make(bot_id="chenlulu", port=17801, chat_id=111):
        (configs_dir / f"{bot_id}.yml").write_text(
            f"id: {bot_id}\ndisplay_name: 陈璐璐\n"
            f"bot_channel_path: ~/.claude/channels/{bot_id}\n"
            f"chat_id: {chat_id}\ndispatcher_port: {port}\n"
            f"namespace_uuid: 11111111-2222-3333-4444-555555555555\n",
            encoding="utf-8")
        d = channels_dir / bot_id
        d.mkdir(parents=True, exist_ok=True)
        return bot_id
    return _make


@pytest.fixture
def port():
    """一个当前空闲的端口号（没人监听），供加 bot 用例做 dispatcher_port。"""
    return free_port()


@pytest.fixture
def port2():
    return free_port()


_NEW_SCRIPT = """#!/bin/sh
# 新版工作副本：读注册表（F4 自检 grep 的就是这个词）
out="$(python3 -m bots_registry --format=sh)" || { echo "bots_registry failed" >&2; exit 1; }
[ -n "$out" ] || { echo "no bots" >&2; exit 1; }
"""
_OLD_SCRIPT = """#!/bin/sh
# 老用户的工作副本：硬编码 bot 列表（F4 要靠自检发现它）
BOTS=("chenlulu:17801")
"""


@pytest.fixture
def restart_script(tmp_path):
    """`(kind) -> 夹具脚本路径`，喂给 §7 `HUB_RESTART_SCRIPT`（§13 A2 新增接缝）。

    该接缝**只**决定 §4.2 C 段第 5 步 `grep -q bots_registry <路径>` 读哪个文件，
    与「执行什么来重启」的 `HUB_RESTART_CMD` 分工写死、互不覆盖。
    用例必须经 `apif(HUB_RESTART_SCRIPT=str(path))` 注入（env 要在门户加载前就位），
    **不再往仓库根摆工作副本，也就没有"本机已有同名文件"的 skip 分支**。
    """
    def _make(kind="new"):
        p = tmp_path / f"restart-bots-{kind}.sh"
        p.write_text(_NEW_SCRIPT if kind == "new" else _OLD_SCRIPT, encoding="utf-8")
        p.chmod(0o755)
        return p
    return _make
