# -*- coding: utf-8 -*-
"""scripts/hub_bootstrap.py 的自测：资产名映射、校验和交叉校验、config 字段级补齐、hub.env 幂等。

跑：python3 -m pytest tests/unit/test_hub_bootstrap.py -q
全程在 tmp 目录里，绝不碰真实 ~/.claude-tgbot。
"""
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("hub_bootstrap", ROOT / "scripts" / "hub_bootstrap.py")
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)


@pytest.mark.parametrize("system,machine,want", [
    ("Darwin", "arm64", "CLIProxyAPI_9.9.9_darwin_aarch64.tar.gz"),
    ("Darwin", "x86_64", "CLIProxyAPI_9.9.9_darwin_amd64.tar.gz"),
    ("Linux", "aarch64", "CLIProxyAPI_9.9.9_linux_aarch64.tar.gz"),
    ("Linux", "x86_64", "CLIProxyAPI_9.9.9_linux_amd64.tar.gz"),
    # Windows 的 platform.machine() 给大写；v7 起 arm64 一律叫 aarch64
    ("Windows", "AMD64", "CLIProxyAPI_9.9.9_windows_amd64.zip"),
    ("Windows", "ARM64", "CLIProxyAPI_9.9.9_windows_aarch64.zip"),
])
def test_资产名六个平台(system, machine, want):
    assert hb.asset_name("9.9.9", system, machine) == want


def test_认不出的平台要报错而不是猜():
    for kw in ({"system": "Plan9", "machine": "x86_64"}, {"system": "Linux", "machine": "riscv64"}):
        with pytest.raises(hb.BootstrapError) as e:
            hb.asset_name("9.9.9", kw["system"], kw["machine"])
        assert e.value.code == "unsupported_platform"


def test_下载地址只认github():
    url = hb.download_url("9.9.9", "a.tar.gz")
    assert url.startswith("https://github.com/router-for-me/CLIProxyAPI/releases/download/v9.9.9/")


def test_校验和首行版本对不上要失败_不是静默放行():
    """[RA] 建议5：改了版本号忘换校验和，是升级时最容易犯的错。"""
    with pytest.raises(hb.BootstrapError) as e:
        hb.read_checksums("0.0.1")
    assert e.value.code == "sha256_version_mismatch"


def test_条目缺失必须失败_不许跳过校验():
    """[R4D] I6：上游无 GPG 签名，sha256 是唯一防线，'找不到就跳过'是最容易写出的退化路径。"""
    ver = hb.read_version()
    with pytest.raises(hb.BootstrapError) as e:
        hb.expected_sha256(ver, "CLIProxyAPI_%s_plan9_mips.tar.gz" % ver)
    assert e.value.code == "checksum_entry_missing"


# ── config.yaml 字段级补齐 ────────────────────────────────

ENV = {"CLIPROXY_PORT": "8317", "CLIPROXY_MGMT_KEY": "mgmt-xyz", "CLIPROXY_API_KEY": "sk-tgbot-abc"}

# cliproxy 自己落盘的形态：启动时把明文 secret-key 换成哈希，管理 API 写 provider 时追加块
CLIPROXY_WRITTEN = '''host: "127.0.0.1"
port: 8317
debug: true
logging-to-file: false
request-log: false
api-keys:
  - "sk-tgbot-abc"
remote-management:
  allow-remote: true
  secret-key: "$2a$10$hashedvalue"
openai-compatibility:
  - name: "user-added-gateway"
    base-url: "https://api.deepseek.com/v1"
    models:
      - name: "deepseek-v4-flash"
        alias: "claude-sonnet-4-5-20250929"
'''


def _run_patch(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    changed, touched = hb.ensure_config(p, ENV, tmp_path / "auths")
    return p.read_text(encoding="utf-8"), changed, touched


def test_重跑把误开的debug与allow_remote拉回来但不碰provider块(tmp_path):
    """A8b：debug:true 每次路由都打半掩码上游 key；allow-remote:true 等于管理口上局域网。

    这两个是骨架字段，不受 A5b「provider 块不动」保护 —— 但用户配的 provider 一个字都不许动。
    """
    out, changed, touched = _run_patch(tmp_path, CLIPROXY_WRITTEN)
    assert changed and "debug" in touched
    assert "debug: false" in out
    assert "  allow-remote: false" in out
    assert "user-added-gateway" in out and "deepseek-v4-flash" in out
    assert '"$2a$10$hashedvalue"' in out, "cliproxy 启动时哈希过的 secret-key 不该被改回明文"


def test_再跑一次不再改动_幂等(tmp_path):
    _run_patch(tmp_path, CLIPROXY_WRITTEN)
    before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    changed, touched = hb.ensure_config(tmp_path / "config.yaml", ENV, tmp_path / "auths")
    assert (changed, touched) == (False, [])
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before


def test_缺字段补齐且api_keys不重复追加(tmp_path):
    out, _, touched = _run_patch(tmp_path, 'port: 8317\napi-keys:\n  - "别人的key"\n')
    assert 'host: "127.0.0.1"' in out and "error-logs-max-files: 0" in out
    assert "别人的key" in out and out.count("sk-tgbot-abc") == 1
    assert "remote-management" in out and "secret-key" in out
    out2, changed2, _ = _run_patch(tmp_path, out)   # 第二遍不再追加
    assert out2.count("sk-tgbot-abc") == 1 and not changed2


def test_行内形式直接报错_不硬改成不合法的yaml(tmp_path):
    """`remote-management: {}` 这种写法硬补会写出重复键的坏 YAML，宁可失败让人手工改。"""
    for text in ('remote-management: {allow-remote: true}\n', 'api-keys: ["k"]\n'):
        with pytest.raises(hb.BootstrapError) as e:
            _run_patch(tmp_path, text)
        assert e.value.code == "config_unparsable"


def test_行尾注释不算值的一部分(tmp_path):
    out, changed, touched = _run_patch(tmp_path, 'debug: false  # 别打开\nport: 8317\n')
    assert "debug" not in touched
    assert "debug: false  # 别打开" in out, "值已经对了就不该重写这一行"


# ── hub.env 与整体引导 ────────────────────────────────────

def test_hub_env_重跑不换口令且保留用户自己加的行(tmp_path):
    """重生成口令 = 用户被自己的门户锁在外面（A5）。"""
    p = tmp_path / "hub.env"
    env1, changed1 = hb.ensure_hub_env(p)
    assert changed1 and len(env1["HUB_ACCESS_PASSWORD"]) >= 16
    p.write_text(p.read_text(encoding="utf-8") + "HUB_ADMIN_PASSWORD=我自己加的\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    env2, changed2 = hb.ensure_hub_env(p)
    assert not changed2 and p.read_text(encoding="utf-8") == before
    assert env2["HUB_ACCESS_PASSWORD"] == env1["HUB_ACCESS_PASSWORD"]


def test_hub_env_兼容标志被改坏了要拉回1(tmp_path):
    """A9b：S0-B 已实证，探测逻辑已删，这个键恒为 1。"""
    p = tmp_path / "hub.env"
    hb.ensure_hub_env(p)
    p.write_text(p.read_text(encoding="utf-8").replace(
        "CLIPROXY_ANTHROPIC_COMPAT=1", "CLIPROXY_ANTHROPIC_COMPAT=0"), encoding="utf-8")
    env, changed = hb.ensure_hub_env(p)
    assert changed and env["CLIPROXY_ANTHROPIC_COMPAT"] == "1"
    assert p.read_text(encoding="utf-8").count("CLIPROXY_ANTHROPIC_COMPAT") == 1


def test_端到端引导_目录700文件600(tmp_path, monkeypatch):
    home = tmp_path / ".claude-tgbot"
    monkeypatch.setenv("CLAUDE_TGBOT_HOME", str(home))
    monkeypatch.delenv("CLIPROXY_PORT", raising=False)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hub_bootstrap.py")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    pw = hb.parse_env_file(home / "hub.env")["HUB_ACCESS_PASSWORD"]
    assert pw not in r.stdout and pw not in r.stderr, "口令不许进 stdout（A10）"
    if os.name != "nt":
        for d in (home, home / "cliproxy", home / "cliproxy" / "logs"):
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, "%s 不是 700" % d
        for f in (home / "hub.env", home / "cliproxy" / "config.yaml"):
            assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, "%s 不是 600" % f
    assert "port: 8317" in (home / "cliproxy" / "config.yaml").read_text(encoding="utf-8")
