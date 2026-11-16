# -*- coding: utf-8 -*-
"""pub-comic-nai 验收用的本机假 NovelAI 生图服务 + 跑脚本的辅助函数。

接缝说明：脚本 --help、assets/default_config.json、README 都没有给出"生图服务地址"的对外配置项，
所以这里走标准网络环境变量这条对外接缝：
  - HTTPS_PROXY / HTTP_PROXY 指向本机 127.0.0.1 上的假代理（端口 0 随机分配）；
  - 假代理收到 CONNECT 后不向外连，直接在本机用测试临时 CA 签的证书做 TLS，自己扮演生图服务；
  - SSL_CERT_FILE / REQUESTS_CA_BUNDLE 指向这张测试 CA。
任何主机名都被本机截下，不会连外网；不改脚本、不 mock 脚本内部。
"""
import io
import json
import os
import re
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SKILL_SRC = REPO / "skills" / "novelai-skill"
FAKE_TOKEN = "pst-FAKE-TEST-TOKEN-0000000000"
FIXED_SEED = 1234567
MOTION_POOL = ("dynamic composition, motion lines", "dynamic angle, motion blur",
               "dynamic pose, sense of motion", "mid-motion, hair and clothes in motion")
MOTION_TOKENS = {t.strip() for m in MOTION_POOL for t in m.split(",")}
GOLDEN_DIR = HERE / "golden"
# 第 9 条金样本的固定输入（0 角色、1 角色，都不写 comic / auto_compose）
GOLDEN_CASES = {
    "zero": {"mode": "new", "prompt": "empty classroom at dusk, warm light, no humans", "characters": []},
    "one": {"mode": "new", "prompt": "standing in a park, sunny day, 1girl",
            "characters": [{"positive": "girl, 2::long hair::, white dress, smiling"}]},
}
SKILL_BLOCK_START = "**一轮要出多张图（mode=new）"

_OPENSSL_CA_EXT = """[req]
distinguished_name=dn
prompt=no
[dn]
CN=pub-comic-nai test CA
[v3_ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
"""


def _png_1x1():
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


PNG = _png_1x1()


def _zip_with_png():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("image_0.png", PNG)
    return buf.getvalue()


class _CA:
    def __init__(self, d):
        self.dir = Path(d)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = self.dir / "ca.key"
        self.pem = self.dir / "ca.pem"
        cfg = self.dir / "ca.cnf"
        cfg.write_text(_OPENSSL_CA_EXT)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-keyout", str(self.key), "-out", str(self.pem), "-config", str(cfg),
                        "-extensions", "v3_ca"], check=True, capture_output=True)
        self._leaf = {}
        self._lock = threading.Lock()

    def context_for(self, host):
        with self._lock:
            if host in self._leaf:
                return self._leaf[host]
            safe = "".join(ch if ch.isalnum() else "_" for ch in host)
            key, csr, crt, ext = (self.dir / (safe + s) for s in (".key", ".csr", ".crt", ".ext"))
            ext.write_text("basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                           "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
                           "authorityKeyIdentifier=keyid\nsubjectAltName=DNS:%s\n" % host)
            subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(csr), "-subj", "/CN=%s" % host], check=True, capture_output=True)
            subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(self.pem),
                            "-CAkey", str(self.key), "-CAcreateserial", "-days", "2", "-out", str(crt),
                            "-extfile", str(ext)], check=True, capture_output=True)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(crt), str(key))
            self._leaf[host] = ctx
            return ctx


def _read_headers(f):
    line = f.readline()
    if not line:
        return None, None
    headers = {}
    while True:
        h = f.readline()
        if not h or h in (b"\r\n", b"\n"):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return line.decode("latin-1").rstrip("\r\n"), headers


def _read_body(f, headers):
    if headers.get("transfer-encoding", "").lower() == "chunked":
        out = b""
        while True:
            n = int(f.readline().split(b";")[0].strip() or b"0", 16)
            if n == 0:
                f.readline()
                return out
            out += f.read(n)
            f.readline()
    n = int(headers.get("content-length", "0") or 0)
    return f.read(n) if n else b""


class FakeNovelAI:
    """记录每个请求：{"method","host","path","headers","body"(bytes)}。"""

    def __init__(self, workdir):
        self.ca = _CA(Path(workdir) / "ca")
        self.requests = []
        self.connects = []
        outer = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                line, headers = _read_headers(self.rfile)
                if not line:
                    return
                method, target, _ = line.split(" ", 2)
                if method == "CONNECT":
                    host = target.rsplit(":", 1)[0]
                    outer.connects.append(target)
                    self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self.wfile.flush()
                    try:
                        tls = outer.ca.context_for(host).wrap_socket(self.connection, server_side=True)
                    except (ssl.SSLError, OSError):
                        return
                    f = tls.makefile("rwb")
                    while True:
                        ln, hd = _read_headers(f)
                        if not ln:
                            break
                        m, p, _ = ln.split(" ", 2)
                        if not outer._serve(f, m, host, p, hd):
                            break
                    try:
                        tls.close()
                    except OSError:
                        pass
                else:
                    host = headers.get("host", "")
                    outer._serve(_RW(self.rfile, self.wfile), method, host, target, headers)

        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = S(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _serve(self, f, method, host, path, headers):
        body = _read_body(f, headers)
        self.requests.append({"method": method, "host": host, "path": path, "headers": headers, "body": body})
        if "image" in path:
            payload, ctype = _zip_with_png(), "application/zip"
        else:
            payload, ctype = b"{}", "application/json"
        f.write(("HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                 % (ctype, len(payload))).encode() + payload)
        f.flush()
        return False

    def gen_requests(self):
        """发往生图接口的请求（路径里带 image 的 POST）。"""
        return [r for r in self.requests if r["method"] == "POST" and "image" in r["path"]]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _RW:
    def __init__(self, r, w):
        self.r, self.w = r, w

    def readline(self, *a):
        return self.r.readline(*a)

    def read(self, *a):
        return self.r.read(*a)

    def write(self, b):
        return self.w.write(b)

    def flush(self):
        return self.w.flush()


PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy",
              "NO_PROXY", "no_proxy")


class Bench:
    """一个用例的沙箱：技能目录临时副本、临时 HOME、状态目录、输出目录、假服务。"""

    def __init__(self, root, fake):
        self.root = Path(root)
        self.fake = fake
        self.skill = self.root / "skill"
        shutil.copytree(str(SKILL_SRC), str(self.skill), ignore=shutil.ignore_patterns(".env*", "__pycache__"))
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.out = self.root / "out"
        for d in (self.home, self.state, self.out):
            d.mkdir(parents=True, exist_ok=True)
        self.script = self.skill / "scripts" / "generate_novelai_image.py"
        self.n = 0

    def config(self, **over):
        cfg = json.loads((self.skill / "assets" / "default_config.json").read_text(encoding="utf-8"))
        cfg["seed"] = FIXED_SEED
        cfg.update(over)
        p = self.root / "cfg.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS and not k.startswith("NOVELAI_")}
        proxy = "http://127.0.0.1:%d" % self.fake.port
        env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
            "ALL_PROXY": proxy, "all_proxy": proxy,
            "SSL_CERT_FILE": str(self.fake.ca.pem), "REQUESTS_CA_BUNDLE": str(self.fake.ca.pem),
            "CURL_CA_BUNDLE": str(self.fake.ca.pem),
            "NOVELAI_BEARER_TOKEN": FAKE_TOKEN, "NOVELAI_SKILL_ROOT": str(self.skill),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        return env

    def run(self, intermediate, config=None, extra=(), result_name=None, timeout=60):
        """写中间稿并跑一次脚本。返回 (CompletedProcess, 结果JSON路径, 本次新增的生图请求列表)。"""
        self.n += 1
        im = self.root / ("im_%d.json" % self.n)
        im.write_text(json.dumps(intermediate, ensure_ascii=False), encoding="utf-8")
        cfg = config if config is not None else self.config()
        res = self.root / (result_name or ("result_%d.json" % self.n))
        before = len(self.fake.gen_requests())
        cmd = [sys.executable, str(self.script), "--intermediate", str(im), "--config", str(cfg),
               "--output-json", str(res), "--state-dir", str(self.state), "--output-dir", str(self.out),
               "--agent-name", "testagent", "--chat-id", "1001", "--ratio", "portrait"] + list(extra)
        cp = subprocess.run(cmd, env=self.env(), capture_output=True, text=True, timeout=timeout,
                            cwd=str(self.root))
        return cp, res, self.fake.gen_requests()[before:]


def body_json(req):
    return json.loads(req["body"].decode("utf-8"))


def normalize(data, root):
    """金样本规整：只处理每次必变的几处，其余逐字节保留。
    1) 本用例临时根目录的绝对路径 -> <ROOT>（image_path / image_markdown / source_request_path /
       last_request_path / history_record_path 等路径字段里出现）；
    2) 图片文件名 novelai_<时间戳>_<随机串>.png -> novelai_<TS>_<ID>.png；
    3) 历史记录文件名 history/<日期_时间>_<随机串>.json -> history/<TS>_<ID>.json；
    4) "generated_at": "<日期_时间>" -> "generated_at": "<TS>"；
    5) 改动前的脚本给 0/1 角色自动补的"运动词"每次从 4 组里随机挑一组（不跟随配置种子，实测 80 次只见这 4 组），
       这 4 组原文 -> <MOTION>（出现在 input / base_caption / final_positive_prompt 里）。"""
    for r in sorted({str(root), os.path.realpath(str(root))}, key=len, reverse=True):
        data = data.replace(r.encode("utf-8"), b"<ROOT>")
    data = re.sub(rb"novelai_\d+_[0-9a-f]+\.png", b"novelai_<TS>_<ID>.png", data)
    data = re.sub(rb"history/\d{8}_\d{6}_[0-9a-f]+\.json", b"history/<TS>_<ID>.json", data)
    data = re.sub(rb'"generated_at": "\d{8}_\d{6}"', b'"generated_at": "<TS>"', data)
    for m in MOTION_POOL:
        data = data.replace(m.encode("utf-8"), b"<MOTION>")
    return data


def tokens(text):
    return [t.strip() for t in text.split(",") if t.strip()]


def auto_added(req, cfg_path, body):
    """正向提示词里既不属于配置正向前缀、也不属于中间稿正文的词 = 脚本自动补的词。"""
    pre = json.loads(Path(cfg_path).read_text(encoding="utf-8"))["positive_prefix"]
    known = set(tokens(pre)) | set(tokens(body))
    return [t for t in tokens(body_json(req)["input"]) if t not in known]


def run_golden_case(root, fake, name):
    """跑一个金样本用例，返回规整后的 {文件名: bytes}。"""
    b = Bench(root, fake)
    cp, res, reqs = b.run(GOLDEN_CASES[name], result_name="result.json")
    assert cp.returncode == 0, "金样本用例 %s 脚本退出码 %s，stderr=%s" % (name, cp.returncode, cp.stderr[-800:])
    assert len(reqs) == 1, "金样本用例 %s 应恰好发出 1 个生图请求，实际 %d" % (name, len(reqs))
    return {
        "%s.request.json" % name: normalize(reqs[0]["body"], b.root),
        "%s.result.json" % name: normalize(res.read_bytes(), b.root),
        "%s.last_request.json" % name: normalize((b.state / "last_request.json").read_bytes(), b.root),
    }


def skill_multi_image_block(text):
    """SKILL.md 里「**一轮要出多张图（mode=new）」开头那段：到它自己的代码块结束，再加紧跟的「- 」要点。"""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(SKILL_BLOCK_START))
    opened = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("```"))
    closed = next(i for i in range(opened + 1, len(lines)) if lines[i].startswith("```"))
    end = closed + 1
    while end < len(lines) and lines[end].strip() == "":
        end += 1
    if end < len(lines) and lines[end].startswith("- "):
        while end < len(lines) and lines[end].startswith("- "):
            end += 1
    else:
        end = closed + 1
    return "\n".join(lines[start:end]) + "\n"
