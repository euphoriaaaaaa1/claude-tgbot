# -*- coding: utf-8 -*-
"""§8 install 可观测断言点 —— 只做"不下载、不注册常驻、不起进程"的那一部分。

跑得起来的三类：
 (1) 配置产物断言：把 HOME 指到 tmp 跑 scripts/hub_bootstrap.py，断言 config.yaml / hub.env；
 (2) 源码级 grep 断言（C2/C4/C5/C6、A10b、B 系列）；
 (3) 版本与校验和文件的一致性（A1/A2/A2b 的静态部分）。
真下载 / 真注册 launchd / 真 kill 进程那几条（A4 A4c A11 A13 A14 B3 B4 B6 C1）
不在本批内，理由见 TEST-PLAN「没覆盖什么」。
"""
import os
import re
import subprocess
import sys

import pytest

from conftest import REPO_ROOT

VERSION_FILE = REPO_ROOT / "configs" / "cliproxy.version"
SHA_FILE = REPO_ROOT / "configs" / "cliproxy.sha256"
BOOTSTRAP = REPO_ROOT / "scripts" / "hub_bootstrap.py"
SH_SCRIPTS = ["install.sh", "scripts/install_cliproxy.sh"]
PS1_SCRIPTS = ["windows/install-cliproxy.ps1", "windows/register-tasks.ps1"]


def _text(rel):
    p = REPO_ROOT / rel
    if not p.exists():
        pytest.fail("§8 要求的文件不存在：%s" % rel)
    return p.read_text(encoding="utf-8", errors="replace")


def test_A1_版本号单行且不带v():
    """⑮：上游两天一版（7.2.147→7.2.148），**不断言具体数字**，只断言格式与一致性。"""
    raw = _text("configs/cliproxy.version")
    assert raw.strip().count("\n") == 0
    assert re.fullmatch(r"\d+\.\d+\.\d+", raw.strip()), \
        "版本串形如 7.2.148（不带 v），实得 %r" % raw.strip()


def test_A2_校验和文件首行是版本注释其余是hash与资产名():
    lines = [l for l in _text("configs/cliproxy.sha256").splitlines() if l.strip()]
    assert lines and lines[0].startswith("# "), "首行必须是 `# <版本号>`"
    for l in lines[1:]:
        assert re.fullmatch(r"[0-9a-f]{64}\s+\S+", l.strip()), "校验和行格式不对：%r" % l


def test_A2b_校验和首行版本必须与版本文件一致():
    """交叉校验（[RA] 建议5）：不一致时脚本应以 sha256_version_mismatch 非零退出。

    这里断言仓库里两份文件本身一致 —— 不一致的仓库状态本身就是缺陷。
    """
    ver = _text("configs/cliproxy.version").strip()
    head = _text("configs/cliproxy.sha256").splitlines()[0].lstrip("# ").strip()
    assert head == ver, "sha256 首行 %r ≠ cliproxy.version %r" % (head, ver)


def test_A3_资产名与架构映射只在hub_bootstrap里定义一处():
    """§8.3 C6：架构映射唯一定义点，否则改一处漏一处，Windows 静默拿到错配置。"""
    hits = []
    for rel in SH_SCRIPTS + PS1_SCRIPTS + ["scripts/hub_bootstrap.py"]:
        p = REPO_ROOT / rel
        if p.exists() and "aarch64" in p.read_text(encoding="utf-8", errors="replace"):
            hits.append(rel)
    assert hits == ["scripts/hub_bootstrap.py"], "aarch64 出现在多处：%s" % hits


def test_C2_安装脚本一个字都不许提settings_json():
    """§8.3 C2 / BRIEF 红线：install 绝不碰开发者本机的 settings.json。"""
    bad = [rel for rel in SH_SCRIPTS + PS1_SCRIPTS + ["scripts/hub_bootstrap.py"]
           if (REPO_ROOT / rel).exists()
           and "settings.json" in (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")]
    assert bad == [], "安装脚本里出现 settings.json：%s" % bad


def test_C4_不得跳过TLS校验():
    pat = re.compile(r"(curl[^\n|]*\s-k\b|--insecure|SkipCertificateCheck|"
                     r"ServerCertificateValidationCallback)")
    bad = [rel for rel in SH_SCRIPTS + PS1_SCRIPTS
           if (REPO_ROOT / rel).exists()
           and pat.search((REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace"))]
    assert bad == [], "安装脚本跳过了 TLS 校验：%s" % bad


def test_C5_下载源只允许github两个host():
    allow = {"github.com", "objects.githubusercontent.com", "api.github.com"}
    bad = []
    for rel in SH_SCRIPTS + PS1_SCRIPTS:
        p = REPO_ROOT / rel
        if not p.exists():
            continue
        for host in re.findall(r"https?://([A-Za-z0-9.\-]+)",
                               p.read_text(encoding="utf-8", errors="replace")):
            if host not in allow and not host.startswith("127.0.0.1"):
                bad.append((rel, host))
    assert bad == [], "安装脚本里出现非 GitHub 下载源：%s" % bad


def test_C3_脚本不写cloudflared配置():
    bad = [rel for rel in SH_SCRIPTS + PS1_SCRIPTS
           if (REPO_ROOT / rel).exists()
           and "cloudflared" in (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")]
    assert bad == [], "安装脚本动了 cloudflare tunnel 配置（cliproxy 会上公网）：%s" % bad


def test_A10b_仓库里的plist与任务XML模板不含凭据变量():
    """§8.1 A10b / §8.2 B5：凭据只能由包装脚本 source hub.env 注入。"""
    creds = ("HUB_ACCESS_PASSWORD", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY")
    bad = []
    for p in list(REPO_ROOT.glob("**/*.plist")) + list(REPO_ROOT.glob("**/*.xml")):
        if ".git" in p.parts or "node_modules" in p.parts:
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        if any(c in t for c in creds):
            bad.append(str(p.relative_to(REPO_ROOT)))
    assert bad == [], "plist/任务 XML 里出现凭据变量：%s" % bad


# ---- 配置产物断言：HOME 指到 tmp 跑 hub_bootstrap.py，绝不碰真实 ~/.claude-tgbot ----

def _run_bootstrap(tmp_path, stub, extra=None):
    if not BOOTSTRAP.exists():
        pytest.fail("§8.3 要求的单点脚本不存在：scripts/hub_bootstrap.py")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "CLAUDE_TGBOT_HOME": str(home / ".claude-tgbot"),
                "CLIPROXY_PORT": str(stub.port), "PYTHONUNBUFFERED": "1"})
    env.update(extra or {})
    r = subprocess.run([sys.executable, str(BOOTSTRAP)], cwd=str(REPO_ROOT), env=env,
                       capture_output=True, text=True, timeout=60)
    return r, home / ".claude-tgbot"


def test_A6_A8_生成的config骨架字段齐全且host是回环(tmp_path, stub):
    r, root = _run_bootstrap(tmp_path, stub)
    assert r.returncode == 0, "hub_bootstrap 失败：%s" % (r.stderr[-600:])
    cfg = (root / "cliproxy" / "config.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s*host:\s*[\"']?127\.0\.0\.1", cfg, re.M), "host 必须是 127.0.0.1"
    for need in ("port:", "remote-management", "allow-remote", "secret-key", "api-keys"):
        assert need in cfg, "config.yaml 缺骨架字段 %s" % need
    assert re.search(r"allow-remote:\s*false", cfg), "管理端口必须只对 localhost"
    assert re.search(r"error-logs-max-files:\s*0", cfg)
    assert re.search(r"logging-to-file:\s*false", cfg)
    assert re.search(r"request-log:\s*false", cfg)
    assert re.search(r"debug:\s*false", cfg), \
        "A8b：debug:true 会把半掩码上游 key（sk-9...7093）打进日志"


def test_A7_目录与文件权限(tmp_path, stub):
    r, root = _run_bootstrap(tmp_path, stub)
    assert r.returncode == 0, r.stderr[-600:]
    import stat as st
    assert st.S_IMODE(os.stat(root).st_mode) == 0o700
    for f in ("hub.env", "cliproxy/config.yaml"):
        assert st.S_IMODE(os.stat(root / f).st_mode) == 0o600, "%s 权限不是 600" % f


def test_A9_hub_env五个键齐全且口令够长(tmp_path, stub):
    r, root = _run_bootstrap(tmp_path, stub)
    assert r.returncode == 0, r.stderr[-600:]
    kv = dict(l.split("=", 1) for l in (root / "hub.env").read_text(encoding="utf-8").splitlines()
              if "=" in l and not l.strip().startswith("#"))
    for k in ("HUB_ACCESS_PASSWORD", "CLIPROXY_PORT", "CLIPROXY_MGMT_KEY",
              "CLIPROXY_API_KEY", "CLIPROXY_ANTHROPIC_COMPAT"):
        assert k in kv, "hub.env 缺 %s" % k
    assert len(kv["HUB_ACCESS_PASSWORD"].strip().strip('"')) >= 16, "自动口令长度不足 16"
    assert "HUB_ADMIN_PASSWORD" not in kv, "§8.1 A9：管理口令不由 install 生成"


def test_A9b_兼容标志直接写1且不得有探测残留(tmp_path, stub):
    """§8.1 A9b 修订 4：S0-B 已实证，**运行时探测逻辑删除**，install 直接写 1。"""
    r, root = _run_bootstrap(tmp_path, stub)
    assert r.returncode == 0, r.stderr[-600:]
    kv = dict(l.split("=", 1) for l in (root / "hub.env").read_text(encoding="utf-8").splitlines()
              if "=" in l)
    assert kv["CLIPROXY_ANTHROPIC_COMPAT"].strip().strip('"') == "1", \
        "探测已删，该键恒为 1"
    blob = str(stub.all_blocks) + str(stub.requests)
    assert "__hubprobe__" not in blob, "出现了探测临时条目 —— 探测逻辑该删没删"


def test_A10_口令值不进stdout(tmp_path, stub):
    r, root = _run_bootstrap(tmp_path, stub)
    kv = dict(l.split("=", 1) for l in (root / "hub.env").read_text(encoding="utf-8").splitlines()
              if "=" in l)
    pw = kv["HUB_ACCESS_PASSWORD"].strip().strip('"')
    assert pw not in r.stdout and pw not in r.stderr


def test_A5_A5b_重跑幂等且不抹掉已配置的provider(tmp_path, stub):
    """[RA] F1：只做骨架字段级补齐，绝不整文件重写 —— 否则用户配的 N 个 provider 消失。"""
    r1, root = _run_bootstrap(tmp_path, stub)
    assert r1.returncode == 0, r1.stderr[-600:]
    cfg = root / "cliproxy" / "config.yaml"
    marker = "\nopenai-compatibility:\n  - name: user-added-gateway\n"
    cfg.write_text(cfg.read_text(encoding="utf-8") + marker, encoding="utf-8")
    pw_before = (root / "hub.env").read_text(encoding="utf-8")
    r2, _ = _run_bootstrap(tmp_path, stub)
    assert r2.returncode == 0, "第二次跑应退出码 0：%s" % r2.stderr[-400:]
    assert "user-added-gateway" in cfg.read_text(encoding="utf-8"), \
        "重跑 install 把用户配好的 provider 抹了（[RA] F1）"
    assert (root / "hub.env").read_text(encoding="utf-8") == pw_before, \
        "重跑重新生成了口令，用户会被自己的门户锁在外面"


@pytest.mark.parametrize("rel", PS1_SCRIPTS)
def test_B_Windows脚本存在且用GetFileHash校验(rel):
    t = _text(rel)
    if "install-cliproxy" in rel:
        assert "Get-FileHash" in t and "SHA256" in t
        assert re.search(r"CLIProxyAPI_.*windows_.*\.zip", t), "缺 Windows 资产名模式"


def test_B4_计划任务跑的是守护循环脚本而不是二进制():
    """§8.2 B4：计划任务没有 KeepAlive，直接跑二进制 = 崩了不再拉起。"""
    t = _text("windows/register-tasks.ps1")
    assert "run-cliproxy.ps1" in t, "任务动作不是 run-cliproxy.ps1（守护循环）"
    assert (REPO_ROOT / "windows" / "run-cliproxy.ps1").exists()


def test_B6_卸载脚本的通配能摘掉新任务():
    t = _text("windows/unregister-tasks.ps1")
    assert "claude-tgbot-*" in t or "claude-tgbot*" in t


def test_A8b_重跑install会把误开的debug改回false(tmp_path, stub):
    """§8.1 A8b：debug 属骨架字段，不受 A5b「provider 块不动」保护。

    实测 debug:true 时每次路由都打半掩码上游 key（`sk-9...7093`），属泄漏面。
    """
    r, root = _run_bootstrap(tmp_path, stub)
    assert r.returncode == 0, r.stderr[-600:]
    cfg = root / "cliproxy" / "config.yaml"
    cfg.write_text(re.sub(r"debug:\s*false", "debug: true", cfg.read_text(encoding="utf-8")),
                   encoding="utf-8")
    r2, _ = _run_bootstrap(tmp_path, stub)
    assert r2.returncode == 0, r2.stderr[-400:]
    assert re.search(r"debug:\s*false", cfg.read_text(encoding="utf-8")), \
        "重跑没把用户误开的 debug:true 改回 false"


@pytest.mark.parametrize("rel,pat", [
    ("install.sh", r"WorkingDirectory"),
    ("scripts/install_cliproxy.sh", r"WorkingDirectory"),
    ("windows/register-tasks.ps1", r"WorkingDirectory"),
])
def test_A11b_三平台常驻单元必须钉死工作目录(rel, pat):
    """§8.1 A11b（新增红线）：v7 启动会往**进程 CWD** 写 static/management.html（2.7 MB）。

    脚本里必须出现 WorkingDirectory 且指向 ~/.claude-tgbot/cliproxy。
    """
    p = REPO_ROOT / rel
    if not p.exists():
        pytest.fail("§8 要求的文件不存在：%s" % rel)
    t = p.read_text(encoding="utf-8", errors="replace")
    if "WorkingDirectory" not in t:
        pytest.fail("%s 没钉死 WorkingDirectory，cliproxy 会往当时的 CWD 拉 2.7 MB" % rel)
    assert "claude-tgbot" in t and "cliproxy" in t


def test_A11b_仓库根不得出现cliproxy拉下来的static目录():
    """CWD 没钉住的直接症状：用户仓库根多出一个 static/management.html。"""
    stray = REPO_ROOT / "static" / "management.html"
    assert not stray.exists(), "仓库根出现了 cliproxy 的 static/management.html（CWD 没钉住）"
