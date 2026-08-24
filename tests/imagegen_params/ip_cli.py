# -*- coding: utf-8 -*-
"""生图脚本的黑盒跑测工具：本地桩服务器 + 子进程调用（普通模块，不是测试文件）。

桩绑在 127.0.0.1 的随机端口上，恒返回 HTTP 400（契约规定 4xx 不重试），所以跑得快，
又能抓到真正发出的请求体。永远不联真实 NovelAI：endpoint 指向桩，令牌用假值。
"""
import json
import os
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "skills" / "novelai-skill" / "scripts" / "generate_novelai_image.py"
FAKE_TOKEN = "pst-FAKE0000000000000000TESTONLY"

# 测试端自备的词表（实现若换同义词需同步更新此表）。默认全部强制全身，只有明确局部词才放行。
FULL_BODY_WORDS = ("full body", "full-body", "fullbody", "head to toe", "full shot",
                   "wide shot", "long shot")
EXPLICIT_LOCAL_WORDS = ("close-up", "closeup", "close up", "extreme close-up",
                        "face focus", "headshot", "portrait",
                        "特写", "拍脸", "大头照", "脸部特写")
NON_FULL_BODY_WORDS = ("medium shot", "upper body", "upper-body", "upper_body", "cowboy shot",
                       "waist up", "bust shot", "between_legs", "lower body",
                       "lower_body", "中景", "近景", "半身", "上半身")
CLOSEUP_WORDS = EXPLICIT_LOCAL_WORDS  # 向后兼容：旧名仍指"明确局部词"
SHOT_WORDS = tuple(dict.fromkeys(
    FULL_BODY_WORDS + EXPLICIT_LOCAL_WORDS + NON_FULL_BODY_WORDS + ("bust", "knee shot")
))
ANGLE_WORDS = ("pov", "from above", "from below", "from side", "from behind",
               "over-the-shoulder", "over the shoulder", "dutch angle", "eye-level",
               "eye level", "bird's-eye", "birds-eye", "low angle", "high angle",
               "first-person", "front view", "back view", "worm's-eye")
DYNAMIC_WORDS = ("dynamic", "motion", "action", "dramatic", "foreshortening",
                 "tension", "energetic", "movement")


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.server.captured.append(self.rfile.read(n))
        body = b'{"message":"stub rejects on purpose"}'
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Stub:
    """端口 0 = 让内核分配空闲端口，绝不会撞上本机在跑的任何服务。"""

    def __enter__(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.captured = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/ai/generate-image" % self.srv.server_address[1]
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()

    @property
    def payloads(self):
        out = []
        for raw in self.srv.captured:
            try:
                out.append(json.loads(raw.decode("utf-8")))
            except Exception:
                out.append({"_unparsable": raw[:200].decode("utf-8", "replace")})
        return out


def base_config(**over):
    cfg = {"provider": "novelai", "model": "nai-diffusion-4-5-full",
           "width": 1024, "height": 1024, "steps": 23, "cfg_scale": 5,
           "sampler": "k_euler_ancestral", "seed": 12345, "prompt_max_tokens": 200,
           "nsfw_prefix": "", "positive_prefix": "", "negative_prefix": "",
           "novelai_parameters": {"negative_prompt": "", "ucPreset": 2,
                                  "sm": False, "sm_dyn": False}}
    cfg.update(over)
    return cfg


def write_styles_root(tmp, styles: dict) -> str:
    """在 tmp 下造一个只含 assets/styles.json 的假 skill 根，供 NOVELAI_SKILL_ROOT 指过去。

    脚本只用这个变量找 styles.json，令牌仍读脚本自己所在目录，所以真实预设绑定不受影响。
    """
    root = Path(tmp) / "skill-root"
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "styles.json").write_text(
        json.dumps(styles, ensure_ascii=False), encoding="utf-8")
    return str(root)


def run_cli(tmp, prompt=None, config=None, args=(), stub_url=None, write_intermediate=True,
            styles_root=None):
    """跑一次生图脚本，返回 (returncode, stdout, stderr)。"""
    if not SCRIPT.exists():
        pytest.skip("生图脚本不存在：%s" % SCRIPT)
    inter = os.path.join(tmp, "intermediate.json")
    if write_intermediate:
        with open(inter, "w", encoding="utf-8") as f:
            json.dump({"prompt": prompt or "", "text": prompt or ""}, f, ensure_ascii=False)
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump(config or base_config(), f, ensure_ascii=False)
    cmd = ["python3", str(SCRIPT), "--intermediate", inter, "--config", cfg,
           "--output-image-path", os.path.join(tmp, "out.png"),
           "--state-dir", os.path.join(tmp, "state"),
           "--output-json", os.path.join(tmp, "out.json")] + list(args)
    env = dict(os.environ)
    # 假令牌显式写进 env：load_local_env 只在 key 不在 env 里时才读 .env.local，
    # 所以本机就算装了真令牌也不会被这套测试用上。
    env["NOVELAI_BEARER_TOKEN"] = FAKE_TOKEN
    env.pop("NOVELAI_JWT", None)
    env.pop("NOVELAI_ACTIVE_STYLE_ID", None)
    # 没显式给假 styles 根时也要隔离：否则会读到本机真实的 styles.json，
    # 测试结果随本机画风绑定漂移。
    env["NOVELAI_SKILL_ROOT"] = styles_root or write_styles_root(
        tmp, {"active": "", "active_by_bot": {}, "styles": []})
    if stub_url:
        env["NOVELAI_IMAGE_ENDPOINT"] = stub_url
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    return p.returncode, p.stdout, mask(p.stderr)


def mask(text):
    """遮蔽任何真实令牌；测试自造的假 token 保持原样，好让"是否回显"可被断言。"""
    def _sub(m):
        return m.group(0) if m.group(0) == FAKE_TOKEN else m.group(1) + "***"
    return re.sub(r"(pst-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+", _sub, text or "")


def tmpdir():
    return tempfile.mkdtemp(prefix="acctest_imagegen_")


def require_payload(stub, rc, stderr):
    """脚本没发出任何请求时跳过（多半是本机缺前置条件），而不是误报失败。"""
    if not stub.payloads:
        pytest.skip("脚本未向桩发出请求（退出码 %s）；末尾 stderr：%s"
                    % (rc, (stderr or "")[-300:]))
    return stub.payloads[0]


def positive_prompt(payload):
    for key in ("input", "prompt"):
        if isinstance(payload.get(key), str):
            return payload[key]
    params = payload.get("parameters") or {}
    return params.get("prompt") or params.get("input") or ""


def params_of(payload):
    return payload.get("parameters") or {}


def hits(text, words):
    low = (text or "").lower()
    return [w for w in words if w in low]
