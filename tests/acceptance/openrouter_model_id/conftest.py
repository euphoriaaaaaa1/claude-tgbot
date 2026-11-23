"""openrouter-model-id-fix 验收测试夹具。

唯一依据：.devflow/openrouter-model-id-fix-接口.md（行为清单 + 错误契约）。
纪律：
  (a) 本目录自带 conftest，不 `from conftest import`（避免顶层模块名冲突）；
  (b) 所有接缝落在临时目录，不碰真实 ~/.claude / 真实 providers.json / 外网；
  (c) 测试只通过对外 HTTP 接口驱动（/login、/api/providers、/api/providers/models[/candidates]）；
      这里只把 `moments.web` 当应用入口取 Flask app，不碰任何内部函数；
  (d) 假上游是标准库起的回环 HTTP 服务，记录收到的请求路径与请求头。
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PASSWORD = "openrouter-model-id-acceptance-pw"

# 与既有 provider_openai_compat 套件同款的接缝组；全部指向临时目录。
SEAM_KEYS = (
    "HOME",
    "CLAUDEBOT_CONFIG_DIR",
    "CLAUDEBOT_CHANNELS_ROOT",
    "CLAUDEBOT_PROVIDER_PATH",
    "CLAUDEBOT_PROXY_DIR",
    "CLAUDEBOT_PROXY_PORT",
    "CLAUDEBOT_PROXY_BIN",
    "CLAUDEBOT_PROXY_LAUNCHCTL",
    "MOMENTS_DISABLE_CONVERGER",
)

# 其中这些是路径，必须落在临时目录下；端口/开关类不参与路径硬闸。
PATH_SEAM_KEYS = (
    "HOME",
    "CLAUDEBOT_CONFIG_DIR",
    "CLAUDEBOT_CHANNELS_ROOT",
    "CLAUDEBOT_PROVIDER_PATH",
    "CLAUDEBOT_PROXY_DIR",
    "CLAUDEBOT_PROXY_BIN",
)


class WebEnv:
    def __init__(self, tmp: Path, configs: Path):
        self.tmp = tmp
        self.configs = configs
        self.provider_path = configs / "providers.json"
        self.app = None


@pytest.fixture(scope="session")
def web_env():
    """临时 HOME/configs/channels + 设定登录口令，然后取应用入口的 Flask app。"""
    tmp = Path(tempfile.mkdtemp(prefix="openrouter_model_id_"))
    home = tmp / "home"
    configs = tmp / "configs"
    channels = tmp / "channels"
    for d in (home, configs, channels):
        d.mkdir(parents=True, exist_ok=True)

    provider_path = configs / "providers.json"
    provider_path.write_text(
        json.dumps({"providers": [], "bindings": {}}, ensure_ascii=False), encoding="utf-8"
    )

    stub = tmp / "cli-proxy-api-stub.sh"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    saved = {k: os.environ.get(k) for k in SEAM_KEYS}
    os.environ.update({
        "HOME": str(home),
        "CLAUDEBOT_CONFIG_DIR": str(configs),
        "CLAUDEBOT_CHANNELS_ROOT": str(channels),
        "CLAUDEBOT_PROVIDER_PATH": str(provider_path),
        "CLAUDEBOT_PROXY_DIR": str(tmp / "provider-proxy"),
        "CLAUDEBOT_PROXY_PORT": "0",            # 不真起代理实例
        "CLAUDEBOT_PROXY_BIN": str(stub),       # 桩脚本，绝不调真实二进制
        "CLAUDEBOT_PROXY_LAUNCHCTL": "off",     # 绝不调 launchctl
        "MOMENTS_DISABLE_CONVERGER": "1",
    })

    # 硬闸：接缝必须在临时目录下，防误碰真实环境。
    for key in PATH_SEAM_KEYS:
        got = Path(os.environ[key]).resolve()
        assert got.is_relative_to(tmp.resolve()), f"接缝 {key} 逃出临时目录: {got}"

    proc = subprocess.run(
        [sys.executable, "-m", "moments.panel_auth", "--set-password"],
        cwd=str(REPO_ROOT),
        env={**os.environ},
        input=f"{PASSWORD}\n{PASSWORD}\n",
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"设置口令失败: {proc.stderr}"

    web = importlib.import_module("moments.web")   # 应用入口
    env = WebEnv(tmp, configs)
    env.app = web.app
    yield env

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture()
def client(web_env):
    c = web_env.app.test_client()
    r = c.post("/login", data={"password": PASSWORD})
    assert r.status_code == 302, f"登录失败: {r.status_code} {r.get_data(as_text=True)[:200]}"
    return c


@pytest.fixture()
def make_provider(client):
    """通过对外接口 POST /api/providers 造一条已存供应商，返回 id。"""

    def _make(*, protocol: str, base_url: str, api_key: str = "sk-openrouter-model-id-test"):
        r = client.post(
            "/api/providers",
            json={
                "name": "or-" + uuid.uuid4().hex[:8],
                "base_url": base_url,
                "api_key": api_key,
                "protocol": protocol,
            },
        )
        assert r.status_code == 201, f"建 provider 失败: {r.status_code} {r.get_data(as_text=True)[:300]}"
        pid = r.get_json()["provider"]["id"]
        assert isinstance(pid, str) and pid
        return pid

    return _make


class FakeUpstream:
    """本机回环假上游：GET 任意路径，记录路径与请求头，按设定回固定 JSON。"""

    def __init__(self):
        self.requests = []          # [{"method", "path", "headers"}]
        self.status = 200
        self.body = b'{"data": []}'
        self.routes = None          # path -> (status, body)，用于分页等多页场景
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append({
                    "method": "GET",
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                if outer.routes is not None and self.path in outer.routes:
                    status, body = outer.routes[self.path]
                else:
                    status, body = outer.status, outer.body
                if not isinstance(body, (bytes, bytearray)):
                    body = str(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def set_json(self, data, **extra):
        payload = {"data": data}
        payload.update(extra)
        self.status, self.body = 200, json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def set_raw(self, body, status=200):
        self.status, self.body = status, body

    def paths(self):
        return [r["path"] for r in self.requests]

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture()
def upstream():
    srv = FakeUpstream()
    yield srv
    srv.close()
