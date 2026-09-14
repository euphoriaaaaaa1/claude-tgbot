"""需求⑤ r6（公开 voicecall/server.py、moments/web.py、moments/post.py）：
启用侧正向对照 + 停用侧反向（INTERFACE §10.4）。HTTP 用 _urlopen 属性注入记录，绝不外发；
每个临时 yml 显式写随机非生产端口，断言请求 URL 端口 == yml dispatcher_port。
"""
import importlib
import os

from _helpers import write_cfg


class _Rec:
    def __init__(self):
        self.urls = []

    def __call__(self, req, *a, **k):
        self.urls.append(req if isinstance(req, str) else req.full_url)

        class R:
            def read(self_inner):
                return b'{"ok":true}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *x):
                return False
        return R()


def cfg(iso, bot, enabled):
    """临时 bot 目录 + configs/<bot>.yml（显式随机端口）。返回 (bot_dir, port)。"""
    d = os.path.join(iso["ch"], bot)
    os.makedirs(os.path.join(d, "inbox"), exist_ok=True)
    with open(os.path.join(d, "access.json"), "w") as f:
        f.write('{"allowFrom": [100], "groups": {}}')
    port = write_cfg(iso["cfg"], bot, enabled=enabled, extra=f"bot_channel_path: {d}\nchat_id: \"100\"\n")
    return d, port


def _files(d, prefix):
    out = []
    for _, _, names in os.walk(d):
        out += [n for n in names if n.startswith(prefix)]
    return out


def _vc(registry):
    import voicecall.server as s
    return importlib.reload(s)


def _mw(registry):
    import moments.web as w
    return importlib.reload(w)


# ---------- voicecall 启用侧 ----------
def test_voicecall_write_inbox_启用bot_返回True_写一个voice文件(iso, registry):
    d, _ = cfg(iso, "bot2", True)
    s = _vc(registry)
    s._urlopen = _Rec()
    assert s._write_inbox("bot2", "合成文本", "voice-") is True
    assert len(_files(d, "voice-")) == 1


def test_voicecall_ensure_worker_启用bot_POST一次_端口等于yml(iso, registry):
    _, port = cfg(iso, "bot2", True)
    s = _vc(registry)
    rec = _Rec()
    s._urlopen = rec
    s._ensure_worker_alive("bot2")
    assert len(rec.urls) == 1 and f":{port}/" in rec.urls[0] and rec.urls[0].rstrip("/").endswith("/ensure_worker"), rec.urls


def test_voicecall_ensure_worker_停用bot_urlopen零次(iso, registry):
    cfg(iso, "bot2", False)
    s = _vc(registry)
    rec = _Rec()
    s._urlopen = rec
    s._ensure_worker_alive("bot2")
    assert rec.urls == []


# ---------- moments/web 启用侧 ----------
def test_moments_web_ensure_worker_启用bot_POST一次_端口等于yml(iso, registry):
    d, port = cfg(iso, "bot3", True)
    w = _mw(registry)
    rec = _Rec()
    w._urlopen = rec
    w._ensure_worker_alive("bot3", "100", d)
    assert len(rec.urls) == 1 and f":{port}/" in rec.urls[0], rec.urls


def test_moments_web_ensure_worker_停用bot_urlopen零次(iso, registry):
    d, _ = cfg(iso, "bot3", False)
    w = _mw(registry)
    rec = _Rec()
    w._urlopen = rec
    w._ensure_worker_alive("bot3", "100", d)
    assert rec.urls == []


# ---------- moments/post._write_moment_image（r6 抽出的入口）----------
def _post(registry):
    import moments.post as p
    return importlib.reload(p)


def test_write_moment_image_停用bot_返回None_不写文件(iso, registry):
    d, port = cfg(iso, "bot2", False)
    bot_cfg = {"id": "bot2", "bot_channel_path": d, "chat_id": "100", "dispatcher_port": port}
    assert _post(registry)._write_moment_image("bot2", bot_cfg, 1, "合成", "public") is None
    assert _files(d, "moment-image-") == []


def test_write_moment_image_启用bot_返回路径_文件名moment_image前缀(iso, registry):
    d, port = cfg(iso, "bot2", True)
    bot_cfg = {"id": "bot2", "bot_channel_path": d, "chat_id": "100", "dispatcher_port": port}
    r = _post(registry)._write_moment_image("bot2", bot_cfg, 1, "合成", "public")
    assert isinstance(r, str) and os.path.isfile(r) and os.path.basename(r).startswith("moment-image-")


def test_write_moment_image_中文bot名停用_返回None(iso, registry):
    d, port = cfg(iso, "陈璐璐", False)
    bot_cfg = {"id": "陈璐璐", "bot_channel_path": d, "chat_id": "100", "dispatcher_port": port}
    assert _post(registry)._write_moment_image("陈璐璐", bot_cfg, 1, "合成", "public") is None
    assert _files(d, "moment-image-") == []
