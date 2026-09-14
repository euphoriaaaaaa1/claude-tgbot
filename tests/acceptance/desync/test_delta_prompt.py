"""缺陷①（公开）：jiwen.deepseek_delta.DELTA_PROMPT 措辞与 compute_delta 错误契约。
不触碰真实打分模型：只用无 key、不可达地址、本地一次性 HTTP 服务。
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from jiwen import deepseek_delta as dd

PERSONA = "合成人设：测试角色"
MSGS = [{"speaker": "主人", "text": "嗯", "is_bot": False}]


def test_prompt_含亲密例外四个必含子串():
    for s in ("敷衍式简短回应→正", "亲密进行中回得短≠冷落", "**connection 亲密例外**", "此例外只影响 connection 一项"):
        assert s in dd.DELTA_PROMPT, f"缺少子串: {s}"


def test_prompt_不再含旧句_被冷落简短回应正():
    assert "被冷落/简短回应→正" not in dd.DELTA_PROMPT


def test_prompt_结构_persona与hints占位在角色简介下片段前():
    p = dd.DELTA_PROMPT
    assert p.index("【角色简介】") < p.index("{persona}") < p.index("{hints_section}") < p.index("【最近对话片段】")


def test_prompt_结构_五字段必须出现准则在亲密例外之后():
    p = dd.DELTA_PROMPT
    assert p.index("【判断准则】") < p.index("**connection 亲密例外**") < p.index("5 个字段都必须出现")


def test_prompt_末行仍要求只输出JSON单行():
    last = [l for l in dd.DELTA_PROMPT.strip().splitlines() if l.strip()][-1]
    assert "只输出 JSON 单行" in last, last


def test_compute_delta_无key_五维全0():
    r = dd.compute_delta(PERSONA, MSGS, "")
    assert r == {"valence": 0, "arousal": 0, "connection": 0, "pride": 0, "energy": 0} or (
        isinstance(r, dict) and len(r) == 5 and all(v == 0 for v in r.values())), r


def test_compute_delta_无消息_五维全0():
    r = dd.compute_delta(PERSONA, [], "sk-fake")
    assert isinstance(r, dict) and len(r) == 5 and all(v == 0 for v in r.values()), r


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_compute_delta_HTTP不可达_返回None():
    port = _free_port()  # 没人监听
    r = dd.compute_delta(PERSONA, MSGS, "sk-fake", base_url=f"http://127.0.0.1:{port}", timeout=2)
    assert r is None


class _Rec(BaseHTTPRequestHandler):
    bodies = []
    status = 500

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Rec.bodies.append(self.rfile.read(n))
        self.send_response(_Rec.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"not json at all")

    def log_message(self, *a):
        pass


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Rec)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def test_compute_delta_服务端500或非JSON_返回None():
    srv = _serve()
    try:
        r = dd.compute_delta(PERSONA, MSGS, "sk-fake", base_url=f"http://127.0.0.1:{srv.server_port}", timeout=5)
    finally:
        srv.shutdown()
    assert r is None


def test_compute_delta_delta_hints非空_请求体含专属准则标题与hints正文():
    _Rec.bodies.clear()
    srv = _serve()
    try:
        dd.compute_delta(PERSONA, MSGS, "sk-fake", delta_hints="亲密进行中回得短不算冷落（合成hint）",
                         base_url=f"http://127.0.0.1:{srv.server_port}", timeout=5)
    finally:
        srv.shutdown()
    assert _Rec.bodies, "没有发出 HTTP 请求"
    text = "\n".join(_strings(json.loads(_Rec.bodies[-1].decode("utf-8"))))
    assert "【该角色的专属判断准则（优先于下方通用准则）】" in text
    assert "亲密进行中回得短不算冷落（合成hint）" in text
    assert PERSONA in text


def test_compute_delta_delta_hints为空_请求体不含专属准则标题():
    _Rec.bodies.clear()
    srv = _serve()
    try:
        dd.compute_delta(PERSONA, MSGS, "sk-fake", base_url=f"http://127.0.0.1:{srv.server_port}", timeout=5)
    finally:
        srv.shutdown()
    text = "\n".join(_strings(json.loads(_Rec.bodies[-1].decode("utf-8"))))
    assert "专属判断准则" not in text
