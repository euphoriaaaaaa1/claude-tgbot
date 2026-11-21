# -*- coding: utf-8 -*-
"""pub-nai-1121 验收的公共工具（普通模块，不是测试文件）。

原则：
- 只走对外入口：命令行、HTTP 接口（Flask 测试客户端，在子进程里调用）、渲染入口 `python -m skill_gen`、文本文件。
- 需要写盘的场景一律在临时目录里的"仓库副本"或"技能目录副本"里做；真实仓库只读。
- 假 NovelAI 服务复用 tests/acceptance/pub_comic_nai/pcn_helpers.py（本机 127.0.0.1 随机端口 + 测试 CA，不连外网）。
- 令牌一律是明显的假值。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SKILL_DIR = REPO / "skills" / "novelai-skill"
SKILL_MD = SKILL_DIR / "SKILL.md"
FAKE_TOKEN = "pst-FAKE-TEST-TOKEN-0000000000"
BOT = "mybot"

sys.path.insert(0, str(HERE.parent / "pub_comic_nai"))
import pcn_helpers as P  # noqa: E402  复用假 NovelAI 服务与金样本规整

# 行为 23：整个 pytest 会话开始前的快照，由 conftest 的 pytest_sessionstart 写入
SKILLS_BEFORE = None

ABSENT = object()   # 画风文件不存在
DIR = object()      # 画风路径上是一个目录


def clean_env(**extra):
    """子进程环境：去掉代理与所有 NOVELAI_ / CLAUDEBOT* 变量，HOME 指向临时目录由调用方给。"""
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith("_proxy") and not k.startswith(("NOVELAI_", "CLAUDEBOT", "BOTLIFE"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def tmp_home():
    return os.path.realpath(tempfile.mkdtemp(prefix="pn-"))


# ───────────────────────── 仓库副本 ─────────────────────────

def light_copy(dst, with_git=False):
    """把真实仓库的"会被提交的工作区内容"复制到 dst：已跟踪 + 未跟踪但未被忽略的文件。
    被忽略的文件（令牌、密钥、本地配置）不会被带过去。with_git=True 时再带上 .git 并在副本里做一次基线提交，
    这样副本里 `git status` 只反映之后发生的变化。"""
    dst = Path(dst)
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=str(REPO),
                         capture_output=True, check=True).stdout
    for rel in filter(None, out.split(b"\0")):
        r = rel.decode("utf-8", "surrogateescape")
        src = REPO / r
        if not (src.is_symlink() or src.is_file()):
            continue
        target = dst / r
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(target), follow_symlinks=False)
    if with_git:
        shutil.copytree(str(REPO / ".git"), str(dst / ".git"), symlinks=True)
        git(dst, "add", "-A")
        git(dst, "commit", "--no-verify", "--allow-empty", "-q", "-m", "base")
    return dst


def git(cwd, *args):
    cp = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True,
        env=clean_env(HOME=tmp_home(), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull))
    assert cp.returncode == 0, "git %s 失败：%s" % (args, cp.stderr)
    return cp.stdout


# ───────────────────────── 技能目录 / 画风文件 ─────────────────────────

def put(path, spec):
    """把 path 摆成指定状态：ABSENT 不存在；DIR 目录；dict 写 JSON；str/bytes 原样写。"""
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(str(path))
    if spec is ABSENT:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec is DIR:
        path.mkdir()
    elif isinstance(spec, dict):
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    elif isinstance(spec, bytes):
        path.write_bytes(spec)
    else:
        path.write_text(spec, encoding="utf-8")


def tree_snapshot(root):
    """整个目录树的指纹：相对路径 -> 类型与内容哈希。用来断言"没写盘"。"""
    snap = {}
    root = Path(root)
    for dp, dns, fns in os.walk(str(root)):
        for n in dns + fns:
            p = Path(dp) / n
            rel = str(p.relative_to(root))
            if p.is_symlink():
                snap[rel] = ("link", os.readlink(str(p)))
            elif p.is_dir():
                snap[rel] = ("dir",)
            else:
                snap[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
    return snap


def style_doc(sid, mark, bot="testagent"):
    """一份合法的画风文件内容：一个预设，正向前缀是独一无二的标记词，便于在请求里认出是谁生效。"""
    return {"active": sid, "active_by_bot": {bot: sid},
            "styles": [{"id": sid, "name": sid, "positive_prefix": mark, "negative_prefix": "", "params": {}}]}


# ───────────────────────── 画风页（Flask 测试客户端，子进程里调用） ─────────────────────────

_PAGE_DRIVER = r'''
import json, sys
from flask import Flask
from moments.styles_routes import styles_bp
app = Flask(__name__)
app.register_blueprint(styles_bp)
c = app.test_client()
out = []
for method, path, payload in json.loads(sys.stdin.read()):
    r = c.get(path) if method == "GET" else c.post(path, json=payload)
    out.append({"status": r.status_code, "json": r.get_json(silent=True), "text": r.get_data(as_text=True)[:300]})
sys.stdout.write("\n@@PN@@" + json.dumps(out))
'''


def page_calls(repo_copy, calls, skill_root=None):
    """在仓库副本里起一个 Python 进程，用 Flask 测试客户端依次调用画风页接口。
    skill_root=None 表示不设 NOVELAI_SKILL_ROOT（走默认值）。返回每个调用的 {status,json,text}。"""
    extra = {"HOME": tmp_home()}
    if skill_root is not None:
        extra["NOVELAI_SKILL_ROOT"] = str(skill_root)
    cp = subprocess.run([sys.executable, "-B", "-c", _PAGE_DRIVER], input=json.dumps(calls), cwd=str(repo_copy),
                        capture_output=True, text=True, timeout=120, env=clean_env(**extra))
    assert "@@PN@@" in cp.stdout, "画风页调用进程没有给出结果：rc=%s stderr=%s" % (cp.returncode, cp.stderr[-600:])
    return json.loads(cp.stdout.split("@@PN@@", 1)[1])


# ───────────────────────── 出图脚本沙箱 ─────────────────────────

class RejectingFake(P.FakeNovelAI):
    """前 reject_first 个生图请求回 HTTP 400，之后恢复正常。请求都会被记录。"""

    def __init__(self, workdir, reject_first=0):
        self.reject_left = reject_first
        super().__init__(workdir)

    def _serve(self, f, method, host, path, headers):
        if self.reject_left > 0 and method == "POST" and "image" in path:
            self.reject_left -= 1
            body = P._read_body(f, headers)
            self.requests.append({"method": method, "host": host, "path": path, "headers": headers, "body": body})
            payload = b'{"statusCode":400,"message":"invalid request"}'
            f.write(("HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                     "Connection: close\r\n\r\n" % len(payload)).encode() + payload)
            f.flush()
            return False
        return super()._serve(f, method, host, path, headers)


class Sandbox(P.Bench):
    """技能目录的临时副本 + 临时 HOME + 状态/输出目录。去掉副本里可能带来的本地画风文件，保证起点干净。"""

    def __init__(self, root, fake):
        super().__init__(root, fake)
        for name in ("styles.local.json",):
            put(self.skill / "assets" / name, ABSENT)
        for p in self.skill.glob(".env*"):
            p.unlink()

    @property
    def local_styles(self):
        return self.skill / "assets" / "styles.local.json"

    @property
    def factory_styles(self):
        return self.skill / "assets" / "styles.json"

    def generated_files(self):
        return sorted(str(p.relative_to(self.root)) for d in (self.out, self.state) for p in d.rglob("*") if p.is_file())


def request_input(req):
    return P.body_json(req)["input"]


def char_captions(body, which):
    """which: 'v4_prompt'（正面）或 'v4_negative_prompt'（负面）。"""
    return body["parameters"][which]["caption"]["char_captions"]


def novelai_lines(stderr):
    return [ln for ln in stderr.splitlines() if ln.startswith("[novelai] ")]


# ───────────────────────── 技能渲染 ─────────────────────────

def render_image_skill(extra_env=None):
    """用 `python -m skill_gen` 在临时 HOME 里渲染 mybot 的 image 技能，返回文本。
    默认不设 CLAUDEBOT_SKILLS_ROOT；extra_env 可以加上它。真实仓库只读。"""
    home = tmp_home()
    try:
        cfg = Path(home) / "cfg"
        cfg.mkdir()
        text = (REPO / "configs" / "_example.yml").read_text(encoding="utf-8")
        text = text.replace("id: my-bot", "id: %s" % BOT).replace("channels/my-bot", "channels/%s" % BOT)
        (cfg / ("%s.yml" % BOT)).write_text(text, encoding="utf-8")
        (Path(home) / ".claude" / "channels" / BOT).mkdir(parents=True)
        env = clean_env(HOME=home, BOTLIFE_DIR=str(REPO), CLAUDEBOT_CONFIG_DIR=str(cfg), HUB_CONFIGS_DIR=str(cfg))
        env.update(extra_env or {})
        cp = subprocess.run([sys.executable, "-B", "-m", "skill_gen", "--bot", BOT, "--mode", "fill",
                             "--names", "image", "--apply"], cwd=str(REPO), env=env,
                            capture_output=True, text=True, timeout=120)
        out = Path(home) / ".claude" / "channels" / BOT / ".claude" / "skills" / ("%s-image" % BOT) / "SKILL.md"
        assert cp.returncode == 0 and out.exists(), "渲染失败 rc=%s %s" % (cp.returncode, (cp.stdout + cp.stderr)[-600:])
        return out.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(home, ignore_errors=True)


@lru_cache(maxsize=1)
def default_rendered():
    """默认环境（不设 CLAUDEBOT_SKILLS_ROOT）渲染一次的结果；返回的是不可变字符串，缓存不引入共享可变状态。"""
    return render_image_skill()


def abs_prefixes(text, skill_name):
    """文本里所有以 /<skill_name> 结尾的绝对路径前缀（即技能目录的上一级）。"""
    return set(re.findall(r"(/[^\s`'\"()<>]*)/%s\b" % re.escape(skill_name), text))


def section_first_line(text, heading_part):
    """含 heading_part 的标题行之后的第一条非空行（小节首条）。"""
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if re.match(r"^#{1,6}\s", ln) and heading_part in ln)
    return next(ln for ln in lines[idx + 1:] if ln.strip())


# ───────────────────────── 行为 23：skills/ 快照 ─────────────────────────

def skills_snapshot():
    st = subprocess.run(["git", "status", "--porcelain", "--ignored", "skills"], cwd=str(REPO),
                        capture_output=True, text=True, check=True).stdout
    hashes = {}
    files = [SKILL_MD] + sorted((SKILL_DIR / "assets").glob("*.json"))
    for p in files:
        hashes[str(p.relative_to(REPO))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"status": st, "hashes": hashes}
