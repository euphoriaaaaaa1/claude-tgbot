# -*- coding: utf-8 -*-
"""scripts/install_cliproxy.sh 的自测：校验和把关、路径穿越、幂等。

不下载真包（57 MB）：造一个 1 KB 的假 tar.gz + 一份假 configs/，
用 HUB_CLIPROXY_LOCAL_PKG 接缝喂给脚本，校验流程一步不少地照跑。
真包（v7.2.148）的端到端只在手工验收里跑一次。
"""
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("hub_bootstrap", ROOT / "scripts" / "hub_bootstrap.py")
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="ps1 那条路在 Windows 上另测")
VER = "9.9.9"


def _sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _make_pkg(path, member="CLIProxyAPI/cli-proxy-api"):
    payload = io.BytesIO(b"#!/bin/sh\necho fake-cliproxy\n")
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(payload.getvalue())
        info.mode = 0o755
        tf.addfile(info, payload)
    return path


@pytest.fixture
def repo(tmp_path):
    """一份最小假仓库：真脚本 + 假版本号/校验和，全程不联网、不碰真 HOME。"""
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    (r / "configs").mkdir()
    (r / "plist-templates").mkdir()
    for rel in ("scripts/hub_bootstrap.py", "scripts/install_cliproxy.sh",
                "plist-templates/cliproxy.plist.tmpl"):
        shutil.copy(ROOT / rel, r / rel)
    (r / "configs" / "cliproxy.version").write_text(VER + "\n", encoding="utf-8")
    return r


def _seal(repo, pkg, asset=None, head=VER, digest=None):
    """写 configs/cliproxy.sha256（首行 `# <ver>`）。"""
    asset = asset or hb.asset_name(VER)
    (repo / "configs" / "cliproxy.sha256").write_text(
        "# %s\n%s  %s\n" % (head, digest or _sha256(pkg), asset), encoding="utf-8")


def _run(repo, tmp_path, pkg, extra=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"HOME": str(home), "CLAUDE_TGBOT_HOME": str(home / ".claude-tgbot"),
                "HUB_SKIP_AUTOSTART": "1", "HUB_CLIPROXY_LOCAL_PKG": str(pkg)})
    env.pop("CLIPROXY_PORT", None)
    env.update(extra or {})
    r = subprocess.run(["bash", str(repo / "scripts" / "install_cliproxy.sh")],
                       capture_output=True, text=True, timeout=120, env=env)
    return r, home / ".claude-tgbot" / "cliproxy"


def test_正常装一遍再跑一遍是幂等的(repo, tmp_path):
    pkg = _make_pkg(tmp_path / hb.asset_name(VER))
    _seal(repo, pkg)
    r1, d = _run(repo, tmp_path, pkg)
    assert r1.returncode == 0, r1.stderr
    assert "sha256 校验通过" in r1.stdout
    assert (d / "cli-proxy-api").exists() and os.access(d / "cli-proxy-api", os.X_OK)
    assert (d / "config.yaml").exists()
    r2, _ = _run(repo, tmp_path, pkg)
    assert r2.returncode == 0 and "跳过下载" in r2.stdout


def test_A4_校验和被篡改就必须失败且不留产物(repo, tmp_path):
    pkg = _make_pkg(tmp_path / hb.asset_name(VER))
    _seal(repo, pkg, digest="0" * 64)
    r, d = _run(repo, tmp_path, pkg)
    assert r.returncode != 0
    assert "checksum_mismatch" in (r.stdout + r.stderr)
    assert not (d / "cli-proxy-api").exists(), "A4c：校验先于解压，失败时目标目录不许有产物"


def test_A4b_校验和条目缺失必须失败_不许走跳过校验的退化路径(repo, tmp_path):
    pkg = _make_pkg(tmp_path / hb.asset_name(VER))
    _seal(repo, pkg, asset="CLIProxyAPI_%s_plan9_mips.tar.gz" % VER)
    r, d = _run(repo, tmp_path, pkg)
    assert r.returncode != 0
    assert "checksum_entry_missing" in (r.stdout + r.stderr)
    assert not (d / "cli-proxy-api").exists()


def test_A2b_版本号与校验和首行对不上要失败(repo, tmp_path):
    pkg = _make_pkg(tmp_path / hb.asset_name(VER))
    _seal(repo, pkg, head="1.2.3")
    r, d = _run(repo, tmp_path, pkg)
    assert r.returncode != 0
    assert "sha256_version_mismatch" in (r.stdout + r.stderr)
    assert not (d / "cli-proxy-api").exists()


def test_归档里有越界路径要拒绝解压(repo, tmp_path):
    """[R4D] B5：压缩包里塞 ../ 就能写到目标目录外面去。"""
    pkg = _make_pkg(tmp_path / hb.asset_name(VER), member="../evil-cli-proxy-api")
    _seal(repo, pkg)
    r, d = _run(repo, tmp_path, pkg)
    assert r.returncode != 0
    assert "unsafe_archive_path" in (r.stdout + r.stderr)
    assert not (d / "cli-proxy-api").exists()


@pytest.mark.skipif(not shutil.which("plutil"), reason="非 macOS，没有 plutil")
def test_macOS常驻注册_plist钉住工作目录且不带任何凭据(repo, tmp_path):
    """把 launchctl 换成桩：真渲染、真 plutil -lint，但绝不真注册到用户会话里。"""
    pkg = _make_pkg(tmp_path / hb.asset_name(VER))
    _seal(repo, pkg)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "launchctl.log"
    (fakebin / "launchctl").write_text(
        '#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % log, encoding="utf-8")
    (fakebin / "launchctl").chmod(0o755)
    r, d = _run(repo, tmp_path, pkg, extra={
        "HUB_SKIP_AUTOSTART": "0", "PATH": "%s:%s" % (fakebin, os.environ["PATH"])})
    assert r.returncode == 0, r.stdout + r.stderr
    plist = tmp_path / "home" / "Library" / "LaunchAgents" / "com.claudebotlife.cliproxy.plist"
    assert plist.exists(), "plist 没落盘"
    assert subprocess.run(["plutil", "-lint", str(plist)], capture_output=True).returncode == 0
    text = plist.read_text(encoding="utf-8")
    assert "<key>WorkingDirectory</key>\n    <string>%s</string>" % d in text, "A11b：工作目录没钉住"
    for cred in ("HUB_ACCESS_PASSWORD", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY"):
        assert cred not in text, "A10b：plist 里出现凭据 %s" % cred
    assert "{CLIPROXY_" not in text, "占位符没渲染干净"
    assert "bootstrap gui/" in log.read_text(encoding="utf-8"), "没走注册这一步"


def test_非UTF8_locale下也能跑完(repo, tmp_path):
    """C locale 里 bash 会把紧跟 $VAR 的中文标点吃进变量名（set -u 直接中断）—— 踩过一次。"""
    pkg = _make_pkg(tmp_path / hb.asset_name(VER))
    _seal(repo, pkg)
    r, d = _run(repo, tmp_path, pkg, extra={"LC_ALL": "C", "LANG": "C"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "unbound variable" not in r.stderr
    assert (d / "cli-proxy-api").exists()


# ── install.sh 的 ⑥ 体检分支（从真文件里抠出来跑，不复刻逻辑，免得改了这边漏那边）──

def _run_section6(tmp_path, unit_rel=None, unit_text=""):
    src = (ROOT / "install.sh").read_text(encoding="utf-8")
    start = src.index("# ── ⑥ provider")
    body = src[start:src.index("\nfi\n", start) + 4]
    body = body.replace("elif bash scripts/install_cliproxy.sh; then", "elif true; then", 1)
    home = tmp_path / "home"
    (home / ".claude-tgbot" / "cliproxy").mkdir(parents=True, exist_ok=True)
    if unit_rel:
        (home / unit_rel).parent.mkdir(parents=True, exist_ok=True)
        (home / unit_rel).write_text(
            unit_text.replace("{DIR}", str(home / ".claude-tgbot" / "cliproxy")), encoding="utf-8")
    stub = 'say(){ :; }\nok(){ echo "OK:$*"; }\ntodo(){ echo "TODO:$*"; }\nset -e\n'
    r = subprocess.run(["bash", "-c", stub + body], cwd=str(ROOT), capture_output=True, text=True,
                       env=dict(os.environ, HOME=str(home),
                                CLAUDE_TGBOT_HOME=str(home / ".claude-tgbot")))
    assert r.returncode == 0, r.stderr
    return r.stdout


PLIST_OK = ("<dict><key>WorkingDirectory</key>\n    <string>{DIR}</string></dict>")
PLIST_BAD = ("<dict><key>ProgramArguments</key><string>{DIR}/cli-proxy-api</string>\n"
             "<key>StandardOutPath</key><string>{DIR}/logs/x.out</string></dict>")
LA = "Library/LaunchAgents/com.claudebotlife.cliproxy.plist"
SVC = ".config/systemd/user/claudebotlife-cliproxy.service"


def test_没有常驻单元时不许报就绪(tmp_path):
    """装上了但没挂常驻 = 开机不起，报"就绪"等于骗人。"""
    out = _run_section6(tmp_path)
    assert "OK:" not in out and "没有常驻单元" in out


def test_plist里工作目录钉住了才算就绪(tmp_path):
    assert "OK:" in _run_section6(tmp_path, LA, PLIST_OK)


def test_plist里只是恰好出现过这个路径不算数(tmp_path):
    """模糊 grep 的老毛病：二进制路径、日志路径里都带这个目录。"""
    out = _run_section6(tmp_path, LA, PLIST_BAD)
    assert "OK:" not in out and "没钉在" in out


def test_systemd_unit同样按工作目录那行判(tmp_path):
    assert "OK:" in _run_section6(tmp_path, SVC, "[Service]\nWorkingDirectory={DIR}\n")
    out = _run_section6(tmp_path, SVC, "[Service]\nExecStart={DIR}/cli-proxy-api\n")
    assert "OK:" not in out and "没钉在" in out
