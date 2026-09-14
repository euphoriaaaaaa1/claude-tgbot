"""需求⑤（公开 scripts/self_initiate.py）：停用 bot 时主动消息脚本自行退出、写 .last、不写 inbox、不 POST。
隔离：HOME=临时目录、HUB_CONFIGS_DIR=临时 configs、SELF_INITIATE_NOW 注入时钟、PATH 前置 stub。
.last=NOW-100 且 .interval 极大：未停用时随机间隔闸也拦住，绝不会真的生成/外发。
"""
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from _helpers import NOW, REPO, write_cfg

SCRIPT = os.path.join(REPO, "scripts", "self_initiate.py")


class _Rec(BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        _Rec.hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST

    def log_message(self, *a):
        pass


def _server():
    srv = HTTPServer(("127.0.0.1", 0), _Rec)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _state_dir(iso):
    p = os.path.join(iso["home"], ".claude", "dispatcher", ".self-initiate-state")
    os.makedirs(p, exist_ok=True)
    return p


def _prep(iso, bot, chat, port, enabled, last=None):
    write_cfg(iso["cfg"], bot, enabled=enabled,
              extra=f"bot_channel_path: {iso['home']}/.claude/channels/{bot}\ndispatcher_port: {port}\nchat_id: \"{chat}\"\n")
    sd = _state_dir(iso)
    if last is not None:
        with open(os.path.join(sd, f"{bot}-{chat}.last"), "w") as f:
            f.write(str(last))
    with open(os.path.join(sd, f"{bot}-{chat}.interval"), "w") as f:
        f.write("999999999")
    return sd


def _run(iso, bot, chat):
    env = dict(os.environ, SELF_INITIATE_NOW=str(int(NOW)))
    return subprocess.run([sys.executable, SCRIPT, bot, chat], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=60)


def _inbox_files(iso, bot):
    p = os.path.join(iso["home"], ".claude", "channels", bot, "inbox")
    return os.listdir(p) if os.path.isdir(p) else []


def test_停用bot_stderr有skip行_exit0(iso):
    _prep(iso, "bot2", "100", 1, enabled=False, last=int(NOW) - 100)
    r = _run(iso, "bot2", "100")
    assert r.returncode == 0, r.stderr
    assert "skip: bot2 disabled (configs/bot2.yml enabled:false)" in r.stderr, r.stderr


def test_停用bot_写last为NOW_interval不动(iso):
    sd = _prep(iso, "bot2", "100", 1, enabled=False, last=int(NOW) - 100)
    _run(iso, "bot2", "100")
    assert open(os.path.join(sd, "bot2-100.last")).read().strip() == str(int(NOW))
    assert open(os.path.join(sd, "bot2-100.interval")).read().strip() == "999999999"


def test_停用bot_last原本不存在_也写为NOW(iso):
    sd = _prep(iso, "bot2", "100", 1, enabled=False, last=None)
    _run(iso, "bot2", "100")
    assert open(os.path.join(sd, "bot2-100.last")).read().strip() == str(int(NOW))


def test_停用bot_不写inbox_不POST_dispatcher(iso):
    _Rec.hits.clear()
    srv = _server()
    try:
        _prep(iso, "bot2", "100", srv.server_port, enabled=False, last=int(NOW) - 100)
        _run(iso, "bot2", "100")
    finally:
        srv.shutdown()
    assert _inbox_files(iso, "bot2") == []
    assert _Rec.hits == []
    assert not os.path.exists(iso["calls"]), "不该调用 claude/tmux/launchctl"


def test_停用bot_重复运行两次_幂等_仍exit0(iso):
    sd = _prep(iso, "bot2", "100", 1, enabled=False, last=int(NOW) - 100)
    r1, r2 = _run(iso, "bot2", "100"), _run(iso, "bot2", "100")
    assert (r1.returncode, r2.returncode) == (0, 0)
    assert open(os.path.join(sd, "bot2-100.last")).read().strip() == str(int(NOW))


def test_中文bot名停用_skip行含中文名(iso):
    _prep(iso, "陈璐璐", "100", 1, enabled=False, last=int(NOW) - 100)
    r = _run(iso, "陈璐璐", "100")
    assert r.returncode == 0 and "skip: 陈璐璐 disabled" in r.stderr, r.stderr


def test_启用bot_无disabled跳过行_间隔闸拦住_不写inbox(iso):
    _prep(iso, "bot2", "100", 1, enabled=True, last=int(NOW) - 100)
    r = _run(iso, "bot2", "100")
    assert r.returncode == 0, r.stderr
    assert "disabled" not in r.stderr
    assert _inbox_files(iso, "bot2") == []


def test_别的bot的yml坏了_不影响本bot停用判定(iso):
    _prep(iso, "bot2", "100", 1, enabled=False, last=int(NOW) - 100)
    with open(os.path.join(iso["cfg"], "other.yml"), "w") as f:
        f.write("id: other\nenabled: [broken")
    r = _run(iso, "bot2", "100")
    assert r.returncode == 0 and "skip: bot2 disabled" in r.stderr, r.stderr


def test_本bot的yml缺失_视为启用_无disabled跳过行(iso):
    sd = _state_dir(iso)
    for suf, val in (("last", int(NOW) - 100), ("interval", 999999999)):
        with open(os.path.join(sd, f"ghost-100.{suf}"), "w") as f:
            f.write(str(val))
    r = _run(iso, "ghost", "100")
    assert "disabled" not in r.stderr and _inbox_files(iso, "ghost") == []
